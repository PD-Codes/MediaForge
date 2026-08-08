"""The Catalogue page: full A-Z lists, bulk selection, and what it refuses.

The parsers run against HTML captured from the live pages, so a redesign shows
up as a failing test rather than as an empty list in the UI. The bulk endpoint
is tested mostly for what it will NOT do -- it is an endpoint that turns a JSON
list into hundreds of scrapes, so every guard on it matters more than the happy
path.
"""

import time

import pytest

from mediaforge import catalogue


# The bulk worker really does reach the download queue now (it used to filter
# every episode away -- see _already_on_disk), so these tests really do create
# queue items. The queue worker started by create_app() would then try to
# download the fake urls and log at ERROR, which another test in the suite
# asserts never happens. Paused for this module: the item is claimed and then
# waits, which is all these tests need to see.
@pytest.fixture(autouse=True)
def _queue_paused(app):
    from mediaforge.web import runtime_state

    with app.app_context():
        runtime_state.set_queue_paused(True)
    yield
    with app.app_context():
        runtime_state.set_queue_paused(False)


# ── Parsers ─────────────────────────────────────────────────────────────────
ANIWORLD_HTML = '''
<div id="seriesContainer"><div class="genre"><ul>
<li><a data-alternative-title="Mo Tian Ji, Demon&#039;s Diary" href="/anime/stream/demons-ascension"
       title=" Demon&#039;s Ascension Stream anschauen"> Demon&#039;s Ascension</a></li>
<li><a data-alternative-title="hack//SIGN" href="/anime/stream/hacksign" title="x">.hack//SIGN</a></li>
<li><a href="/anime/stream/no-alt-title" title="x">No Alt Title</a></li>
</ul></div></div>
'''

STO_HTML = '''
<ul class="series-list small">
  <li class="series-item" data-search="...und dann noch paula und dann noch paula">
    <a href="/serie/und-dann-noch-paula">...und dann noch Paula</a>
  </li>
  <li class="series-item" data-search="breaking bad">
    <a href="/serie/breaking-bad">Breaking Bad</a>
  </li>
</ul>
'''


def test_aniworld_parser():
    entries = catalogue.parse_aniworld_catalogue(ANIWORLD_HTML, "https://aniworld.to")
    assert [e["title"] for e in entries] == \
        ["Demon's Ascension", ".hack//SIGN", "No Alt Title"]
    assert entries[0]["url"] == "https://aniworld.to/anime/stream/demons-ascension"
    # Alternate titles are lower-cased on the server: the filter runs on every
    # keystroke over ~11k entries and must not lower-case them each time.
    assert "demon's diary" in entries[0]["alt"]
    assert entries[2]["alt"] == ""


def test_sto_parser():
    entries = catalogue.parse_sto_catalogue(STO_HTML, "https://serienstream.to")
    assert [e["title"] for e in entries] == ["...und dann noch Paula", "Breaking Bad"]
    assert entries[1]["url"] == "https://serienstream.to/serie/breaking-bad"


def test_parsers_drop_duplicates_and_survive_junk():
    doubled = ANIWORLD_HTML + ANIWORLD_HTML
    assert len(catalogue.parse_aniworld_catalogue(doubled, "https://aniworld.to")) == 3
    # A challenge page or a redesign yields nothing rather than garbage.
    assert catalogue.parse_aniworld_catalogue("<html>nope</html>", "https://aniworld.to") == []
    assert catalogue.parse_sto_catalogue("", "https://serienstream.to") == []


def test_catalogue_urls_are_resolvable():
    """Every entry has to be something the provider registry can turn into a
    scraper -- an entry that cannot be queued has no business in this list."""
    from mediaforge.providers import resolve_provider

    entries = (catalogue.parse_aniworld_catalogue(ANIWORLD_HTML, "https://aniworld.to")
               + catalogue.parse_sto_catalogue(STO_HTML, "https://serienstream.to"))
    for entry in entries:
        assert resolve_provider(entry["url"]) is not None


def test_the_sto_catalogue_url_is_not_the_dead_domain():
    """mirrors.canonical_host("sto") is still "s.to", the domain the project
    deactivated. Entry urls are STORED (queue, AutoSync), so they must be
    written the way the rest of the app writes them."""
    assert "serienstream.to" in catalogue.STO_CATALOGUE_URL
    assert not catalogue.STO_CATALOGUE_URL.startswith("https://s.to")


# ── Registry ────────────────────────────────────────────────────────────────
def test_builtin_catalogues_exist():
    all_ = catalogue.all_catalogues()
    assert set(all_) >= {"aniworld", "sto"}
    # Ids match the source ids used everywhere else, so the page can reuse the
    # existing enabled/disabled state instead of inventing its own.
    from mediaforge.web.source_policy import BUILTIN_SEARCH_SOURCES
    known = {s["id"] for s in BUILTIN_SEARCH_SOURCES}
    assert set(all_) <= known


def test_a_module_cannot_shadow_a_builtin():
    with pytest.raises(ValueError):
        catalogue.register_catalogue("mod", "aniworld", "Fake", lambda: [])


def test_module_catalogue_round_trip():
    catalogue.register_catalogue("mod-1", "mysite", "MySite", lambda: [], kind="series")
    try:
        assert "mysite" in catalogue.all_catalogues()
    finally:
        catalogue.unregister_catalogue("mod-1")
    assert "mysite" not in catalogue.all_catalogues()


# ── Routes ──────────────────────────────────────────────────────────────────
def test_page_and_sources(as_user):
    client = as_user("admin")
    assert client.get("/catalogue").status_code == 200
    data = client.get("/api/catalogue/sources").get_json()
    ids = {s["id"] for s in data["sources"]}
    assert {"aniworld", "sto"} <= ids


def test_unknown_catalogue_is_404(as_user):
    assert as_user("admin").get("/api/catalogue/nope").status_code == 404


def test_state_is_one_request_for_the_whole_list(as_user):
    """The page renders up to eleven thousand rows and cannot ask per title."""
    data = as_user("admin").get("/api/catalogue/state").get_json()
    assert isinstance(data["queued"], list)
    assert isinstance(data["autosync"], list)


@pytest.mark.parametrize("body, expected", [
    ({}, 400),                                              # no source
    ({"urls": []}, 400),                                    # empty urls
    ({"source": "aniworld"}, 400),                          # no urls
    ({"urls": ["x"], "mode": "wat"}, 400),                   # bad mode
])
def test_bulk_rejects_malformed_requests(as_user, body, expected):
    resp = as_user("admin").post("/api/catalogue/bulk", json=body)
    assert resp.status_code == expected


# ── Storing a catalogue for the route tests ─────────────────────────────────
# The bulk and resolve endpoints validate submitted urls with an INDEXED query
# against the stored rows now, instead of loading every source's full list into
# Python. Stubbing catalogue_store.all_entries() therefore no longer stubs
# anything they use -- so these tests put real rows in the real table, which is
# what they should have done all along: the query being exercised is the one
# that runs in production.
@pytest.fixture()
def stored(app):
    def _store(per_source):
        from mediaforge.web.db import save_catalogue
        with app.app_context():
            for source_id, entries in per_source.items():
                save_catalogue(source_id, entries)
    return _store


def test_bulk_refuses_urls_that_are_not_in_the_catalogue(as_user, stored):
    """The catalogue is the closed set of things this page may act on. Without
    this check the endpoint would scrape any url it is handed -- a request
    forgery tool with a queue attached."""
    # Validated against the STORED rows, not a live fetch: a POST must not be
    # able to trigger two multi-megabyte downloads just to answer "is this url
    # in a catalogue".
    stored({"aniworld": [
        {"title": "A", "url": "https://aniworld.to/anime/stream/a", "alt": ""}]})
    resp = as_user("admin").post("/api/catalogue/bulk", json={
        "urls": ["https://evil.invalid/x", "https://aniworld.to/anime/stream/not-listed"],
    })
    assert resp.status_code == 400
    assert "no known urls" in resp.get_json()["error"]


def test_there_is_no_selection_ceiling(as_user, monkeypatch, stored):
    """Marking the whole catalogue is a legitimate thing to want. The cost of a
    huge selection is handled where it belongs -- one series at a time in a
    background job that can be stopped -- not by refusing the request."""
    from mediaforge.web.routes import catalogue as routes

    assert not hasattr(routes, "MAX_BULK_SELECTION")

    fake = [{"title": str(i), "url": "https://aniworld.to/anime/stream/s%d" % i, "alt": ""}
            for i in range(1200)]
    stored({"aniworld": fake})
    monkeypatch.setattr(routes, "all_catalogues", lambda: {"aniworld": {}})
    # Nothing is actually expanded: the worker is what would scrape.
    monkeypatch.setattr(routes.catalogue_worker, "start_job",
                        lambda *a, **k: {"id": "test", "total": len(a[1])})

    resp = as_user("admin").post("/api/catalogue/bulk",
                                 json={"urls": [e["url"] for e in fake]})
    assert resp.status_code == 202
    assert resp.get_json()["total"] == 1200


def test_a_selection_may_span_several_sources(as_user, monkeypatch, stored):
    """The page shows one merged list, so there is no "the" source to check
    against -- each url is resolved against whichever catalogue holds it."""
    from mediaforge.web.routes import catalogue as routes

    per_source = {
        "aniworld": [{"title": "A", "url": "https://aniworld.to/anime/stream/a", "alt": ""}],
        "sto": [{"title": "B", "url": "https://serienstream.to/serie/b", "alt": ""}],
    }
    monkeypatch.setattr(routes, "all_catalogues", lambda: {k: {} for k in per_source})
    stored(per_source)
    captured = {}

    def _start(source, urls, *a, **k):
        captured["source"] = source
        captured["urls"] = urls
        return {"id": "test", "total": len(urls)}

    monkeypatch.setattr(routes.catalogue_worker, "start_job", _start)

    resp = as_user("admin").post("/api/catalogue/bulk", json={
        "urls": ["https://aniworld.to/anime/stream/a", "https://serienstream.to/serie/b"]})
    assert resp.status_code == 202
    assert len(captured["urls"]) == 2
    # Both sources are recorded on the job, so the progress card can say what
    # it is working through.
    assert captured["source"] == "aniworld+sto"


def test_unknown_bulk_job_is_404(as_user):
    assert as_user("admin").get("/api/catalogue/bulk/deadbeef").status_code == 404


def test_cancelling_a_job_that_is_not_running_fails_cleanly(as_user):
    resp = as_user("admin").post("/api/catalogue/bulk/deadbeef/cancel")
    assert resp.status_code == 400


# ── Worker ──────────────────────────────────────────────────────────────────
def test_the_bulk_worker_is_in_the_operations_view():
    """It is a background worker like any other, and an operator who cannot see
    it has no way to tell a stuck expansion from a finished one."""
    from mediaforge.web.worker_registry import WORKERS

    assert "catalogue" in WORKERS
    entry = WORKERS["catalogue"]
    # No stall watchdog: idle for days is this worker's normal, healthy state.
    assert entry["stall"] is None
    assert entry["link"] == "/catalogue"


# ── The page's own controls ─────────────────────────────────────────────────
# Rendering assertions rather than behaviour ones: the JS is what implements
# the rail, the chips and the sort toggle, but every one of them needs a hook
# in the template, and a hook that silently disappears is a feature that
# silently stops existing.
def test_the_page_renders_its_controls(as_user):
    html = as_user("admin").get("/catalogue").get_data(as_text=True)
    for hook in ('id="catSort"',            # A-Z / Z-A toggle
                 'id="catStatusChips"',     # library / queued / sync filters
                 'id="catRail"',            # A-Z jump rail
                 'id="catRailBubble"',      # the letter shown while dragging
                 'id="catActions"',         # the bar that hides when empty
                 'id="catModalCard"',
                 'class="modal-backdrop"',  # details modal header image
                 'class="cat-list-wrap"'):
        assert hook in html, hook


def test_labels_built_in_js_are_translatable(as_user):
    """Anything the JS renders has to come through CAT_I18N -- a string typed
    into catalogue.js never reaches the .po catalogue and stays English in
    every language, forever."""
    import re

    html = as_user("admin").get("/catalogue").get_data(as_text=True)
    for key in ("chip_hint", "chip_empty", "only_selection",
                "sort_az", "sort_za", "sort_label"):
        assert re.search(r"\b%s:\s*\"" % key, html), key


def test_the_page_does_not_link_out_to_the_source_site(as_user):
    """Deliberate: the Catalogue page never offers to open aniworld.to or
    serienstream.to in a browser tab."""
    html = as_user("admin").get("/catalogue").get_data(as_text=True)
    assert 'id="catModalOpen"' not in html
    assert "Open on the site" not in html


def test_a_module_may_supply_its_own_source_colour():
    """The merged list marks every row with its source's colour, so a
    third-party catalogue has to be able to set one -- and only a literal hex
    may get through, because the value is rendered into a style attribute."""
    from mediaforge import catalogue as cat

    try:
        cat.register_catalogue("mod-colour", "mysite", "MySite",
                               lambda: [], color="#7c5cff")
        assert cat.all_catalogues()["mysite"]["color"] == "#7c5cff"

        # CSS injected through the colour is dropped, not escaped.
        cat.register_catalogue("mod-colour", "mysite", "MySite",
                               lambda: [], color="red; background:url(x)")
        assert cat.all_catalogues()["mysite"]["color"] == ""
    finally:
        cat.unregister_catalogue("mod-colour")

    # Built-ins carry theirs too, so the page has one source of truth.
    assert cat.BUILTIN_CATALOGUES["aniworld"]["color"].startswith("#")
    assert cat.BUILTIN_CATALOGUES["sto"]["color"].startswith("#")


def test_the_sources_endpoint_reports_the_colour(as_user):
    data = as_user("admin").get("/api/catalogue/sources").get_json()
    by_id = {s["id"]: s for s in data["sources"]}
    assert by_id["aniworld"]["color"].startswith("#")
    assert by_id["sto"]["color"].startswith("#")


# ── Persistence ─────────────────────────────────────────────────────────────
# The lists live in SQLite now (web/db/catalogue_cache.py) and are served from
# there while a refresh runs behind the answer (web/catalogue_store.py). What
# these check is the part that is easy to get wrong and invisible when it is:
# that a FAILED refresh never costs the user the list they already had.
def test_a_stored_catalogue_survives_and_is_served_without_fetching(app, monkeypatch):
    from mediaforge.web import catalogue_store, db

    rows = [{"title": "Breaking Bad", "url": "https://serienstream.to/serie/bb", "alt": "bb"},
            {"title": "Attack on Titan", "url": "https://aniworld.to/anime/stream/aot", "alt": "snk"}]
    with app.app_context():
        db.init_catalogue_cache_db()
        db.save_catalogue("sto", rows)

        calls = []
        monkeypatch.setattr(catalogue_store, "_sources", lambda: {
            "sto": {"label": "SerienStream",
                    "fetch": lambda: calls.append(1) or rows}})

        entries, meta = catalogue_store.get_entries("sto")
        assert [e["title"] for e in entries] == ["Attack on Titan", "Breaking Bad"]
        assert meta["count"] == 2
        # Fresh, so nothing was refetched to answer this.
        assert calls == []


def test_a_failed_refresh_keeps_the_previous_list(app):
    """The single most important property of the store: a source being down
    must not turn a perfectly good catalogue into an error page."""
    from mediaforge.web import db

    with app.app_context():
        db.init_catalogue_cache_db()
        db.save_catalogue("aniworld", [
            {"title": "A", "url": "https://aniworld.to/anime/stream/a", "alt": ""}])
        db.mark_catalogue_failed("aniworld", "HTTPError: 503")

        assert len(db.load_catalogue("aniworld")) == 1
        info = db.catalogue_meta("aniworld")["aniworld"]
        assert info["status"] == "failed"
        assert info["count"] == 1          # the previous count, not zero


def test_resolved_ids_survive_a_refresh(app):
    """Re-resolving thousands of titles because a site added five is exactly
    the cost this table exists to avoid."""
    from mediaforge.web import db

    with app.app_context():
        db.init_catalogue_cache_db()
        db.save_catalogue("aniworld", [
            {"title": "A", "url": "https://aniworld.to/anime/stream/a", "alt": ""}])
        db.set_catalogue_ids("aniworld", "https://aniworld.to/anime/stream/a",
                             tmdb_id="1396")
        db.save_catalogue("aniworld", [
            {"title": "A", "url": "https://aniworld.to/anime/stream/a", "alt": ""},
            {"title": "B", "url": "https://aniworld.to/anime/stream/b", "alt": ""}])

        by_url = {e["url"]: e for e in db.load_catalogue("aniworld")}
        assert by_url["https://aniworld.to/anime/stream/a"]["tmdb_id"] == "1396"
        assert by_url["https://aniworld.to/anime/stream/b"]["tmdb_id"] == ""
        # And the backfill only sees the one that has never been looked up.
        # Filtered by source: the tests share one database, and other cases in
        # this file store catalogues of their own.
        todo = [t for t in db.catalogue_entries_without_ids(50)
                if t["source_id"] == "aniworld"]
        assert [t["url"] for t in todo] == ["https://aniworld.to/anime/stream/b"]


def test_an_empty_fetch_never_replaces_a_good_catalogue(app, monkeypatch):
    """A challenge page or a redesign parses to zero entries. Storing that
    would swap a working catalogue for nothing at all."""
    from mediaforge.web import catalogue_store, db

    with app.app_context():
        db.init_catalogue_cache_db()
        db.save_catalogue("aniworld", [
            {"title": "A", "url": "https://aniworld.to/anime/stream/a", "alt": ""}])
        catalogue_store._do_refresh("aniworld", {"fetch": lambda: []})

        assert len(db.load_catalogue("aniworld")) == 1
        assert db.catalogue_meta("aniworld")["aniworld"]["status"] == "failed"


def test_the_status_endpoint_reports_freshness_and_progress(as_user):
    data = as_user("admin").get("/api/catalogue/status").get_json()
    assert "sources" in data and "refreshing" in data and "ids" in data
    for key in ("total", "checked", "resolved", "running"):
        assert key in data["ids"], key
    for src in data["sources"]:
        for key in ("id", "label", "count", "fetched_at", "failed", "refreshing"):
            assert key in src, key


def test_the_refresh_endpoint_answers_immediately(as_user, monkeypatch):
    """202 and return: the work happens in the background and the page
    follows it through /api/catalogue/status."""
    from mediaforge.web.routes import catalogue as routes

    monkeypatch.setattr(routes.catalogue_store, "refresh_stale", lambda force=False: 2)
    resp = as_user("admin").post("/api/catalogue/refresh", json={})
    assert resp.status_code == 202
    assert resp.get_json()["started"] == 2

    assert as_user("admin").post("/api/catalogue/refresh",
                                 json={"source": "nope"}).status_code == 404


def test_the_refresh_worker_is_in_the_operations_view():
    """Distinct from the bulk-expansion worker: they fail for different
    reasons and an operator needs to tell them apart."""
    from mediaforge.web.worker_registry import WORKERS

    assert "catalogue_sync" in WORKERS
    assert WORKERS["catalogue_sync"]["stall"] is None
    assert WORKERS["catalogue_sync"]["link"] == "/catalogue"


# ── "Do I already have this?" ───────────────────────────────────────────────
def test_the_page_loads_the_library_index_app_js_owns():
    """The bug this guards against: app.js only calls loadDownloadedFolders()
    from the start page's own loaders and from the search modal. On /catalogue
    it never ran, so `downloadedFolders` stayed empty, `mediascanActive` stayed
    false, and isDownloaded() answered "no" for everything -- the "In library"
    badge could not appear on this page at all, whatever was on disk."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] /
          "src/mediaforge/web/static/catalogue.js").read_text(encoding="utf-8")
    assert "window.loadDownloadedFolders" in js, \
        "the Catalogue page must populate app.js's library index itself"
    # Both indexes, because app.js picks between them: a Plex/Jellyfin library
    # reports through mediascan and has no download folders at all.
    assert "_isDownloadedByTitle" in js
    assert "window.isDownloaded" in js


def test_downloaded_episodes_use_the_download_modals_own_markup():
    """Same classes as renderSeasons() in app.js, from cards.css. Reused rather
    than reinvented so the two views cannot drift apart -- and, like the
    download modal, nothing at all is said about episodes that are MISSING."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src/mediaforge/web"
    js = (root / "static/catalogue.js").read_text(encoding="utf-8")
    css = (root / "static/cards.css").read_text(encoding="utf-8")

    assert 'class="ep-downloaded"' in js
    assert 'class="season-downloaded"' in js
    # The classes have to actually exist in the globally loaded sheet.
    assert ".ep-downloaded" in css and ".season-downloaded" in css
    # No "you have none of this" line, and none of the bespoke markup it used.
    for gone in ("cat-eplist-summary", "cat-season-have", "cat-ep-have",
                 "nothing_on_disk", "have_n_of_m"):
        assert gone not in js, gone


# ── Title -> id resolution ──────────────────────────────────────────────────
def test_a_low_confidence_tmdb_hit_is_not_stored(app, monkeypatch):
    """The single most important rule of the backfill. TMDB's /search/multi
    answers almost every query with SOMETHING, and storing that would mean
    confidently matching a title against the wrong show -- worse than the
    fuzzy matching it replaces, and invisible when it happens. The entry is
    still stamped as checked so it is not retried on every pass."""
    from mediaforge.web import catalogue_ids, db

    with app.app_context():
        db.init_catalogue_cache_db()
        db.save_catalogue("aniworld", [
            {"title": "Obscure Show", "url": "https://aniworld.to/anime/stream/obs", "alt": ""}])

        seen = {}

        def _lookup(title, **kw):
            seen.update(kw)
            # This is what lookup_media() returns when require_confident
            # rejects a hit: None, not a dict with found=False.
            return None

        monkeypatch.setattr(catalogue_ids, "resolve_entry",
                            catalogue_ids.resolve_entry)  # keep the real one
        monkeypatch.setattr("mediaforge.web.tmdb_cache.lookup_media", _lookup)

        result = catalogue_ids.resolve_entry(
            "aniworld", "https://aniworld.to/anime/stream/obs", "Obscure Show")

        assert seen.get("require_confident") is True
        assert result == {"tmdb_id": "", "imdb_id": ""}
        entry = db.load_catalogue("aniworld")[0]
        assert entry["tmdb_id"] == ""
        # Checked, so the next pass skips it.
        assert not [e for e in db.catalogue_entries_without_ids(50)
                    if e["url"] == "https://aniworld.to/anime/stream/obs"]


def test_a_confident_hit_is_stored_with_both_ids(app, monkeypatch):
    from mediaforge.web import catalogue_ids, db

    with app.app_context():
        db.init_catalogue_cache_db()
        db.save_catalogue("sto", [
            {"title": "Breaking Bad", "url": "https://serienstream.to/serie/bb", "alt": ""}])

        monkeypatch.setattr(
            "mediaforge.web.tmdb_cache.lookup_media",
            lambda title, **kw: {"found": True, "tmdb_id": 1396,
                                 "raw_details": {"external_ids": {"imdb_id": "tt0903747"}}})

        result = catalogue_ids.resolve_entry(
            "sto", "https://serienstream.to/serie/bb", "Breaking Bad")
        assert result == {"tmdb_id": "1396", "imdb_id": "tt0903747"}

        entry = db.load_catalogue("sto")[0]
        assert entry["tmdb_id"] == "1396" and entry["imdb_id"] == "tt0903747"


def test_a_tmdb_outage_does_not_mark_the_entry_checked(app, monkeypatch):
    """Somebody else's outage must not cost us the entry: leaving it unchecked
    is exactly what makes the next pass try again."""
    from mediaforge.web import catalogue_ids, db

    with app.app_context():
        db.init_catalogue_cache_db()
        db.save_catalogue("aniworld", [
            {"title": "X", "url": "https://aniworld.to/anime/stream/x", "alt": ""}])

        def _boom(title, **kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr("mediaforge.web.tmdb_cache.lookup_media", _boom)
        result = catalogue_ids.resolve_entry(
            "aniworld", "https://aniworld.to/anime/stream/x", "X")

        assert result["error"] is True
        assert [e for e in db.catalogue_entries_without_ids(50)
                if e["url"] == "https://aniworld.to/anime/stream/x"]


def test_resolve_refuses_a_url_that_is_not_in_any_catalogue(as_user):
    """Otherwise this is a "look anything up on TMDB for me" endpoint with the
    app's API key attached."""
    resp = as_user("admin").post("/api/catalogue/resolve",
                                 json={"url": "https://evil.invalid/x"})
    assert resp.status_code == 404
    assert as_user("admin").post("/api/catalogue/resolve", json={}).status_code == 400


def test_the_id_worker_is_in_the_operations_view():
    from mediaforge.web.worker_registry import WORKERS

    assert "catalogue_ids" in WORKERS
    assert WORKERS["catalogue_ids"]["stall"] is None


def test_ids_are_written_one_batch_per_transaction(app):
    """The backfill resolves in parallel, so a commit per title would be 13k
    of them, each contending with whatever the UI is doing."""
    from mediaforge.web import db

    with app.app_context():
        db.init_catalogue_cache_db()
        db.save_catalogue("sto", [
            {"title": "A", "url": "https://serienstream.to/serie/a", "alt": ""},
            {"title": "B", "url": "https://serienstream.to/serie/b", "alt": ""},
        ])
        written = db.set_catalogue_ids_bulk([
            ("sto", "https://serienstream.to/serie/a", "1", "tt1"),
            ("sto", "https://serienstream.to/serie/b", "", ""),   # checked, no hit
        ])
        assert written == 2

        by_url = {e["url"]: e for e in db.load_catalogue("sto")}
        assert by_url["https://serienstream.to/serie/a"]["tmdb_id"] == "1"
        assert by_url["https://serienstream.to/serie/b"]["tmdb_id"] == ""
        # Both are stamped, so neither comes back as work to do.
        assert not [e for e in db.catalogue_entries_without_ids(50)
                    if e["source_id"] == "sto"]


def test_the_backfill_does_not_monopolise_the_shared_tmdb_budget():
    """`lookup_media` is rate-limited process-wide and that budget is shared
    with the modal a user is looking at right now. Four workers use roughly a
    third of it; a number anywhere near the limit would make the UI wait."""
    from mediaforge.web import catalogue_ids
    from mediaforge.web.tmdb_cache import _tmdb_rl

    assert 1 <= catalogue_ids.LOOKUP_WORKERS <= 8
    # Each entry is several TMDB calls (search, details, providers, videos),
    # so the worker count has to stay well under the per-second budget.
    assert catalogue_ids.LOOKUP_WORKERS * 4 < getattr(_tmdb_rl, "rate", 40)


def test_status_chips_filter_for_a_category_not_against_it():
    """They started out inverted -- on by default, click to hide -- and read as
    broken: pressing "In library" showed everything you do NOT have. They are
    positive filters now, and each one carries the count of what pressing it
    would show, so a category with nothing in it says so instead of silently
    doing nothing."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] /
          "src/mediaforge/web/static/catalogue.js").read_text(encoding="utf-8")
    assert "onStatus" in js
    # The inverted state must be gone entirely, including from the saved
    # preferences -- a restored `offStatus` would now mean the opposite.
    assert "offStatus.has" not in js
    assert "onlySelected =" not in js
    assert "cat-chip-count" in js
    assert "chip_empty" in js
    # The expensive library check is cached rather than redone per keystroke.
    assert "computeLibraryFlags" in js


def test_the_page_loads_app_js(as_user):
    """The bug behind "I have thousands of titles and the catalogue says 0".

    app.js owns the download-folder list, the server-side alias index, the
    Plex/Jellyfin index and the three-stage matching in downloadedFolderFor().
    This page read those globals without ever loading the file that defines
    them, so every library check answered no -- silently, because they are all
    behind `typeof` guards. It has to load first, before catalogue.js."""
    html = as_user("admin").get("/catalogue").get_data(as_text=True)
    import re

    scripts = re.findall(r"/static/(app|catalogue)\.js", html)
    assert "app" in scripts, "app.js is not loaded on the Catalogue page"
    assert scripts.index("app") < scripts.index("catalogue"), \
        "app.js must load before catalogue.js"


def test_rows_are_merged_on_the_tmdb_id_and_never_on_the_title():
    """A title both sites carry becomes one row with a pill per site. Folded on
    an identical TMDB id ONLY: two sites spelling a title the same way is not
    evidence that it is the same show -- a remake shares its name with the
    original, and merging those would hide one show's episodes behind the
    other's name with no way to tell."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] /
          "src/mediaforge/web/static/catalogue.js").read_text(encoding="utf-8")
    assert "combineByTmdbId" in js
    assert "variants" in js and "applyActiveVariant" in js
    # The picked site travels with the mark, and every site's url counts as
    # known when the selection is submitted.
    assert "pickSource" in js
    assert "e.variants.forEach((v) => known.add(v.url))" in js


def test_the_page_offers_a_preferred_source(as_user):
    """Pressing a pill picks per row; the dropdown decides for everything the
    user has not pressed. Without one, marking five hundred merged rows would
    mean five hundred decisions."""
    html = as_user("admin").get("/catalogue").get_data(as_text=True)
    assert 'id="catPreferred"' in html
    assert 'id="catPreferredField"' in html
    import re
    for key in ("pick_source", "first_available"):
        assert re.search(r"\b%s:\s*\"" % key, html), key


def test_either_url_of_a_merged_pair_is_accepted(as_user, monkeypatch, stored):
    """A merged row is one row with two urls behind it, and the pill decides
    which one travels. The endpoint must accept whichever it gets -- it
    validates against the stored catalogues, which hold both."""
    from mediaforge.web.routes import catalogue as routes

    pair = {
        "aniworld": [{"title": "Attack on Titan", "alt": "",
                      "url": "https://aniworld.to/anime/stream/aot"}],
        "sto": [{"title": "Attack on Titan", "alt": "",
                 "url": "https://serienstream.to/serie/aot"}],
    }
    monkeypatch.setattr(routes, "all_catalogues", lambda: {k: {} for k in pair})
    stored(pair)

    captured = {}
    monkeypatch.setattr(routes.catalogue_worker, "start_job",
                        lambda source, urls, *a, **k: captured.update(
                            source=source, urls=urls) or {"id": "t", "total": len(urls)})

    for url, expected_source in (
            ("https://aniworld.to/anime/stream/aot", "aniworld"),
            ("https://serienstream.to/serie/aot", "sto")):
        resp = as_user("admin").post("/api/catalogue/bulk", json={"urls": [url]})
        assert resp.status_code == 202, url
        assert captured["urls"] == [url]
        assert captured["source"] == expected_source


def test_there_is_exactly_one_refresh_control(as_user):
    """The status strip used to carry a second one a few pixels below the
    toolbar's, doing exactly the same thing."""
    html = as_user("admin").get("/catalogue").get_data(as_text=True)
    assert 'id="catRefresh"' in html
    assert 'id="catStatusRefresh"' not in html


def test_refreshing_also_wakes_the_id_backfill(as_user, monkeypatch):
    """"Update" means "bring everything up to date". Refetching the lists is
    only half of it: the ids are what merges the two sites' rows and what
    decides "already in my library", and the backfill otherwise sits in an
    idle wait for up to fifteen minutes.

    Spies on the call rather than on the event: a worker really is running in
    these tests, and it clears the event the moment it wakes -- so asserting
    on the flag is a race the test would lose about half the time."""
    from mediaforge.web import catalogue_ids
    from mediaforge.web.routes import catalogue as routes

    monkeypatch.setattr(routes.catalogue_store, "refresh_stale", lambda force=False: 0)
    woken = []
    monkeypatch.setattr(catalogue_ids, "start", lambda: woken.append(True))

    assert as_user("admin").post("/api/catalogue/refresh", json={}).status_code == 202
    assert woken, "the id worker was not woken"


def test_storing_a_catalogue_wakes_the_id_backfill(app, monkeypatch):
    """New titles have just landed and their ids are what make them mergeable;
    they must not wait for the next idle timeout."""
    from mediaforge.web import catalogue_ids, catalogue_store

    woken = []
    monkeypatch.setattr(catalogue_ids, "wake", lambda: woken.append(True))
    with app.app_context():
        catalogue_store._do_refresh("aniworld", {"fetch": lambda: [
            {"title": "Woken", "url": "https://aniworld.to/anime/stream/woken", "alt": ""}]})
    assert woken


def test_an_idle_wait_can_be_interrupted():
    """Without this the whole feature is "press Update, wait a quarter of an
    hour, see whether anything happened"."""
    import threading

    from mediaforge.web import catalogue_ids

    catalogue_ids._stop.clear()
    catalogue_ids._kick.clear()
    done = threading.Event()

    def _sleeper():
        catalogue_ids._idle(30)      # would block for half a minute
        done.set()

    threading.Thread(target=_sleeper, daemon=True).start()
    catalogue_ids.wake()
    assert done.wait(timeout=5), "wake() did not interrupt the idle wait"
    catalogue_ids._kick.clear()


# ── The bulk worker, actually run ───────────────────────────────────────────
# Every other test in this file stubs start_job out, which is precisely why
# nobody noticed that _run() died on its first line: catalogue_worker lives in
# mediaforge/web/ but its imports were written with the depth a module in
# mediaforge/web/routes/ needs, so `..db` resolved to the non-existent
# `mediaforge.db`. The whole feature had never worked. These run the thread.
import types


class _FakeEpisode:
    def __init__(self, url, number, downloaded):
        self.url, self.episode_number = url, number
        # The REAL shape. `is_downloaded` is not a boolean on any model: every
        # one of them returns models/common/common.check_downloaded()'s dict,
        # and a non-empty dict is always truthy. The bulk worker read it as a
        # flag, so every episode of every series counted as already on disk,
        # the whole selection came back "skipped" and not one queue item was
        # ever created -- a configured bulk action started no download at all.
        # The old fake used a plain bool and could never have caught that.
        self.is_downloaded = {
            "exists": bool(downloaded),
            "video_langs": set(),
            "audio_langs": set(),
            "height": 0, "width": 0, "bitrate": 0,
        }


class _FakeSeason:
    def __init__(self, number):
        self.season_number = number
        # Episode 1 of each season is already on disk.
        self.episodes = [_FakeEpisode("https://aniworld.to/e/%d/%d" % (number, i), i, i == 1)
                         for i in (1, 2, 3)]


class _FakeSeries:
    def __init__(self, url=None):
        self.url, self.title = url, "Testserie"
        self.poster_url = "http://example.invalid/p.jpg"
        self.seasons = [_FakeSeason(1), _FakeSeason(2)]


@pytest.fixture()
def fake_provider(monkeypatch):
    import mediaforge.providers as provs
    monkeypatch.setattr(provs, "resolve_provider",
                        lambda url: types.SimpleNamespace(series_cls=_FakeSeries))
    return provs


def _run_job(app, **kwargs):
    from mediaforge.web import catalogue_worker

    job = catalogue_worker.start_job(**kwargs)
    for _ in range(100):
        time.sleep(0.05)
        state = catalogue_worker.get_job(job["id"])
        if state["status"] != "running":
            return state
    raise AssertionError("bulk job did not finish")


def test_a_queue_job_really_reaches_the_queue(app, fake_provider):
    from mediaforge.web import db

    url = "https://aniworld.to/anime/stream/queue-me"
    with app.app_context():
        state = _run_job(app, source="aniworld", urls=[url], language="German Dub",
                         provider="VOE", mode="queue", missing_only=True)
        assert state["status"] == "finished"
        assert state["failed"] == 0, state["errors"]
        assert state["queued"] == 1
        # Six episodes, two of them already on disk.
        assert state["episodes"] == 4

        item = [i for i in db.get_queue() if i.get("series_url") == url]
        assert len(item) == 1
        assert item[0]["title"] == "Testserie"
        assert item[0]["language"] == "German Dub"
        assert item[0]["provider"] == "VOE"
        assert item[0]["source"] == "catalogue"


def test_an_autosync_job_is_stored_and_not_duplicated(app, fake_provider):
    from mediaforge.web import db

    url = "https://aniworld.to/anime/stream/sync-me"
    with app.app_context():
        state = _run_job(app, source="aniworld", urls=[url], language="German Dub",
                         provider="VOE", mode="autosync")
        assert state["status"] == "finished" and state["failed"] == 0, state["errors"]
        assert state["queued"] == 1

        jobs = [j for j in db.get_autosync_jobs() if j.get("series_url") == url]
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Testserie"
        assert jobs[0]["language"] == "German Dub"
        assert jobs[0]["provider"] == "VOE"
        assert jobs[0].get("cover_url")

        # An existing job is left alone rather than reset: it may carry a
        # filter, a custom path or a language the user chose deliberately.
        again = _run_job(app, source="aniworld", urls=[url], language="English Dub",
                         provider="Vidoza", mode="autosync")
        assert again["skipped"] == 1 and again["queued"] == 0
        after = [j for j in db.get_autosync_jobs() if j.get("series_url") == url]
        assert len(after) == 1
        assert after[0]["language"] == "German Dub"


# ── "Already on disk", read correctly ───────────────────────────────────────
def test_a_check_downloaded_dict_is_not_a_boolean():
    """The regression itself, in one line.

    Every model's `is_downloaded` is check_downloaded()'s dict. Truthiness of
    that dict says nothing at all -- it is non-empty either way -- so a bulk
    expansion that used it as a flag dropped EVERY episode and produced an
    empty queue item, which is what "the catalogue starts no download" was.
    """
    from mediaforge.web.catalogue_worker import _already_on_disk

    absent = {"exists": False, "video_langs": set(), "audio_langs": set(),
              "height": 0, "width": 0, "bitrate": 0}
    present = dict(absent, exists=True)

    assert bool(absent), "guard: the dict is truthy even when nothing is there"
    assert _already_on_disk(types.SimpleNamespace(is_downloaded=absent)) is False
    assert _already_on_disk(types.SimpleNamespace(is_downloaded=present)) is True
    # Older/simpler models and third-party ones may answer with a plain bool.
    assert _already_on_disk(types.SimpleNamespace(is_downloaded=False)) is False
    assert _already_on_disk(types.SimpleNamespace(is_downloaded=True)) is True
    # An episode that cannot answer at all is "not there", never "skip it".
    assert _already_on_disk(types.SimpleNamespace()) is False


def test_only_missing_keeps_the_episodes_that_are_missing(fake_provider):
    """Two seasons of three, episode 1 of each already on disk -> four left."""
    from mediaforge.web.catalogue_worker import _episodes_for

    title, episodes, total = _episodes_for("https://aniworld.to/anime/stream/x", True)
    assert title == "Testserie"
    assert total == 6
    assert len(episodes) == 4
    assert all("/1" != url[-2:] for url in episodes)

    _, everything, total_again = _episodes_for("https://aniworld.to/anime/stream/x", False)
    assert len(everything) == total_again == 6


def test_a_series_without_any_episodes_is_reported_not_swallowed(app, monkeypatch):
    """A series page that yields nothing is a FAILURE with a reason, not a
    silent "skipped" -- the counter that told the user nothing about why their
    selection produced an empty queue."""
    import mediaforge.providers as provs

    class _Empty:
        def __init__(self, url=None):
            self.url, self.title, self.seasons = url, "Leer", []

    monkeypatch.setattr(provs, "resolve_provider",
                        lambda url: types.SimpleNamespace(series_cls=_Empty))
    with app.app_context():
        state = _run_job(app, source="aniworld",
                         urls=["https://aniworld.to/anime/stream/empty"],
                         language="German Dub", provider="VOE", mode="queue",
                         missing_only=True)
    assert state["failed"] == 1 and state["queued"] == 0
    assert "no episodes" in state["errors"][0]["error"]


def test_the_state_endpoint_uses_the_queues_own_status_words():
    """`pending` and `paused` are not download_queue statuses (see the CHECK
    constraint in db/queue.py); a pause is global, not per item. Filtering on
    them matched nothing, so a series that had just been queued never got its
    "Queued" badge -- which reads exactly like nothing happened."""
    import inspect

    from mediaforge.web.db import queue as queue_db
    from mediaforge.web.routes import catalogue as cat_routes

    src = inspect.getsource(cat_routes.register_catalogue_routes)
    body = src.split("def api_catalogue_state")[1].split("@app.route")[0]
    # Only lines that actually run -- the comment above it names the wrong
    # words on purpose, to say why they were wrong.
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
    assert '("queued", "running")' in code
    assert "pending" not in code and "paused" not in code

    # And those words have to be the ones the schema allows.
    assert "'queued'" in queue_db._CREATE_QUEUE_TABLE
    assert "'pending'" not in queue_db._CREATE_QUEUE_TABLE
    assert "'paused'" not in queue_db._CREATE_QUEUE_TABLE


# ── The details modal's backdrop ────────────────────────────────────────────
# ── Static assets, read from disk ───────────────────────────────────────────
# The details modal is now the download modal's markup driven by modals.css,
# and the seasons load one at a time. Neither is visible in a rendered page --
# both are decisions in the js/css -- so these read the files.
from pathlib import Path as _Path

_STATIC = _Path(__file__).resolve().parents[1] / "src/mediaforge/web/static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


def test_the_details_modal_is_the_download_modals_own_markup(as_user):
    """Not "styled after" it -- the same classes, so the same rules in
    modals.css paint both and the two cannot drift. The private copy
    (.cat-modal-backdrop, a scrim on the card, its own poster sizing, a
    blurred-poster fallback) is what kept this dialog looking like a different
    application."""
    html = as_user("admin").get("/catalogue").get_data(as_text=True)
    card = html.split('id="catModalCard"')[1].split("cat-modal-footer")[0]
    for cls in ('class="modal-backdrop"', 'class="modal-header cat-modal-header"',
                'class="modal-poster-col"', 'class="modal-meta"',
                'class="genres"', 'class="desc"'):
        assert cls in card, cls
    # The cover is IN the header, lying on the picture -- it used to be
    # rendered into the body, below everything the backdrop covered.
    header = card.split('class="modal-header')[1].split("cat-modal-scroll")[0]
    assert 'id="catModalPoster"' in header

    css = _read("catalogue.css")
    assert ".cat-modal-backdrop" not in css, "the private copy is back"
    assert "is-poster" not in css, "the blurred-poster fallback is back"


def test_the_backdrop_height_is_measured_with_the_apps_own_variable():
    """--cat-bd-h was this page's private name for --mf-backdrop-h, and nothing
    ever wrote it, so the modal always used the CSS fallback band. It follows
    the header now, through the same variable app.js uses."""
    js = _read("catalogue.js")
    assert 'setProperty("--mf-backdrop-h"' in js
    assert "--cat-bd-h" not in js
    assert "ResizeObserver" in js


def test_the_cineinfo_settings_are_actually_loaded():
    """upgradeBackdropFromTmdb() reads window.cineinfoSettings and returns on
    its first line without it. Nothing on this page ever loaded them, so the
    TMDB backdrop could never appear at all -- the blurred poster everybody
    saw was the fallback, permanently."""
    js = _read("catalogue.js")
    assert "loadCineinfoSettings()" in js


def test_a_season_loads_its_own_episodes():
    """Every season's episodes used to be fetched in parallel the moment the
    dialog opened -- one live scrape per season, simultaneously, at a site
    behind DDoS-Guard. A twelve-season series meant twelve requests before
    anything was on screen."""
    js = _read("catalogue.js")
    episodes = js.split("function loadEpisodes(")[1].split("async function toggleSeason(")[0]
    assert "/api/episodes" not in episodes, "the season list still fetches episodes"
    assert "Promise.all" not in episodes
    # One request, in the handler, for the season that was opened.
    toggle = js.split("async function toggleSeason(")[1].split("\n  }")[0]
    assert "/api/episodes" in toggle
    assert 'dataset.loaded === "1"' in toggle, "an open/close cycle refetches"


# ── Performance, where it is measurable ─────────────────────────────────────
def test_the_url_lookups_use_their_index(app):
    """EXPLAIN QUERY PLAN rather than a stopwatch: a plan that says SCAN is the
    bug, whatever the row count happens to be on the machine running this."""
    from mediaforge.web.db import save_catalogue
    from mediaforge.web.db._core import get_db

    with app.app_context():
        save_catalogue("aniworld", [
            {"title": "T%d" % i, "url": "https://aniworld.to/anime/stream/p%d" % i,
             "alt": ""} for i in range(50)])
        conn = get_db()
        try:
            def plan(sql, params=()):
                return " ".join(r["detail"] for r in
                                conn.execute("EXPLAIN QUERY PLAN " + sql, params))

            # find_catalogue_entry / catalogue_sources_for_urls
            by_url = plan("SELECT source_id FROM catalogue_entries WHERE url IN (?, ?)",
                          ("x", "x/"))
            assert "idx_catalogue_entries_url" in by_url, by_url

            # load_catalogue's ORDER BY
            listing = plan("SELECT title, url FROM catalogue_entries "
                           "WHERE source_id = ? ORDER BY title COLLATE NOCASE, url",
                           ("aniworld",))
            assert "USE TEMP B-TREE" not in listing, listing

            # The id backfill's queue. The ORDER BY used to be an expression
            # ("ids_checked_at IS NOT NULL, ids_checked_at"), which produces the
            # same order -- SQLite sorts NULLs first anyway -- while making the
            # sort unindexable: a full scan plus a temp B-tree every twelve
            # seconds for the hour and a half the backfill runs.
            backfill = plan(
                "SELECT source_id, url, title FROM catalogue_entries "
                "WHERE ids_checked_at IS NULL OR (tmdb_id IS NULL AND ids_checked_at < ?) "
                "ORDER BY ids_checked_at LIMIT ?", (0, 5))
            assert "USE TEMP B-TREE" not in backfill, backfill
        finally:
            conn.close()


def test_finding_one_entry_does_not_read_the_others(app, monkeypatch):
    from mediaforge.web import db
    from mediaforge.web.db import find_catalogue_entry, save_catalogue

    with app.app_context():
        save_catalogue("aniworld", [
            {"title": "Wanted", "url": "https://aniworld.to/anime/stream/w", "alt": ""}])
        loads = []
        monkeypatch.setattr(db, "load_catalogue", lambda *a, **k: loads.append(1) or [])
        assert find_catalogue_entry("https://aniworld.to/anime/stream/w")["title"] == "Wanted"
        # Trailing slash, and a url nobody stored.
        assert find_catalogue_entry("https://aniworld.to/anime/stream/w/")
        assert find_catalogue_entry("https://evil.invalid/x") == {}
        assert not loads


def test_the_library_check_reduces_the_folder_list_once():
    """downloadedFolderFor() lower-cased and loose-keyed every folder again for
    every title it was asked about. Fine for the cards a start page renders,
    ~4.7 seconds of solid main-thread work for the Catalogue page's 13k rows
    against a few hundred folders -- twice, because the page runs two passes."""
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    assert "function _buildFolderIndex()" in app_js
    body = app_js.split("function downloadedFolderFor(title) {")[1].split("\n}")[0]
    assert "_dlFoldersLower" in body and "_dlFoldersLoose" in body
    assert "downloadedFolders.find(" not in body, "still scanning the raw list"
    # The TITLE is still normalised once per call, which is the point. What
    # must not be here is normalising a FOLDER -- that is the per-title-times-
    # per-folder work the index replaces.
    for folder_side in ("normalizeQuotes(f", "_looseTitleKey(f)", "f.toLowerCase()"):
        assert folder_side not in body, folder_side


def test_the_library_pass_waits_for_the_library():
    """Without the index every one of the thirteen thousand answers is "no",
    and the whole pass runs again the moment /api/downloaded-folders lands --
    the same seconds of work, twice, exactly when the page is trying to become
    usable."""
    js = _read("catalogue.js")
    assert "if (!libIndexReady) return;" in js
    assert "libIndexReady = true;" in js


def test_the_virtual_rows_are_the_height_the_stylesheet_gives_them():
    """The spacer is rows x this number. Too small and the list is shorter than
    its content: the last rows cannot be scrolled to. It said 44/54 at 720px
    against a stylesheet that says 46 and, below 860px, a 64px minimum."""
    js = _read("catalogue.js")
    css = _read("catalogue.css")

    def const(name):
        return int(js.split("const %s = " % name)[1].split(";")[0])

    row = css.split(".cat-row {")[1].split("}")[0]
    assert "height: %dpx;" % const("ROW_HEIGHT_DESKTOP") in row

    phone = css.split("@media (max-width: 860px) {")[1]
    phone_row = phone.split(".cat-row {")[1].split("}")[0]
    assert "min-height: %dpx;" % const("ROW_HEIGHT_MOBILE") in phone_row
    assert '"(max-width: 860px)"' in js, "the JS breakpoint left the stylesheet's"


def test_a_selection_of_urls_that_no_longer_exist_is_pruned():
    """The stored selection was only ever added to. A url from a source that
    was later switched off stayed for good: the counter kept reporting things
    nothing could submit, and on a phone the action bar could never reach zero
    and so never got out of the way again."""
    js = _read("catalogue.js")
    assert "function pruneStoredState()" in js
    load_all = js.split("entries = combineByTmdbId(merged);")[1].split("\n  }")[0]
    assert "pruneStoredState();" in load_all


def test_a_failed_source_list_offers_a_retry_instead_of_an_empty_page():
    js = _read("catalogue.js")
    body = js.split("async function loadSources() {")[1].split("\n  }")[0]
    assert "showRetry();" in body


def test_resolving_one_id_does_not_rebuild_thirteen_thousand_rows():
    """An id only merges rows if some OTHER row already carries the same one.
    Rebuilding unconditionally meant every details open on an unresolved title
    re-merged the whole list and re-ran every library check."""
    js = _read("catalogue.js")
    body = js.split("async function resolveIds(url) {")[1].split("\n  }")[0]
    assert "const twin =" in body
    assert "if (twin) {" in body


def test_the_row_hover_survives_a_light_theme():
    """A white wash is invisible on a light theme and on every light theme
    pack -- which is exactly what this stylesheet's own header warns about."""
    css = _read("catalogue.css")
    hover = css.split(".cat-row:hover")[1].split("}")[0]
    assert "var(--bg-hover)" in hover
    assert "rgba(255" not in hover


# ── Three things that only show up on screen ────────────────────────────────
def test_the_backdrop_does_not_read_a_let_declared_global_off_window():
    """app.js declares `let cineinfoSettings`, and a top-level `let` never
    becomes a property of the global object -- so `window.cineinfoSettings` is
    undefined however loaded the settings are. Reading it that way meant the
    backdrop check saw no data every single time, and the dialog showed no
    picture at all."""
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    assert "let cineinfoSettings" in app_js, \
        "if this became a var/window assignment, simplify the reader below"

    js = _read("catalogue.js")
    # In CODE, not in the comment that explains why it must not be there.
    code = "\n".join(ln for ln in js.splitlines()
                     if not ln.lstrip().startswith(("*", "//", "/*")))
    assert "window.cineinfoSettings" not in code
    assert "function cineinfoSettingsOrNull()" in js
    # The bare identifier resolves across two classic scripts; the typeof guard
    # is for a page that does not load app.js at all.
    assert 'typeof cineinfoSettings !== "undefined"' in js


def test_marking_a_row_does_not_replay_the_other_rows_checkboxes():
    """.chb-main plays wave-bounce/draw-check whenever a NEW element enters the
    document already :checked. Rebuilding the slice with innerHTML on every
    render therefore made every already-marked row look freshly clicked -- on
    each new mark, and on every scroll frame."""
    js = _read("catalogue.js")
    render = js.split("  function render() {")[1].split("\n  }")[0]
    assert "rowsHost.innerHTML = html" not in render
    assert "makeRowNode()" in render and "paintRow(" in render

    paint = js.split("function paintRow(node, e) {")[1].split("\n  }")[0]
    # Assigning the same value is free; assigning a different one is what may
    # animate, which is right exactly when the state really changed.
    assert "if (node._box.checked !== isSel)" in paint

    css = (_STATIC / "forms.css").read_text(encoding="utf-8")
    assert "animation: wave-bounce" in css, "the animation this is about is gone"


def test_the_dialog_is_anchored_to_the_top_so_it_cannot_jump():
    """Centring a dialog whose height changes moves BOTH edges: collapsing a
    season took ~200px out of the middle, so the card jumped up by half of that
    and the whole thing appeared to flicker. The download modal's overlay is
    top-anchored for the same reason (place-items: start center)."""
    css = _read("catalogue.css")
    overlay = css.split(".cat-modal-overlay {\n")[1].split("}")[0]
    assert "align-items: flex-start;" in overlay
    card = css.split(".cat-modal-card {")[1].split("}")[0]
    assert "margin: auto;" not in card
