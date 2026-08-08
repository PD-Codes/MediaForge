"""The Catalogue page: full A-Z lists, bulk selection, and what it refuses.

The parsers run against HTML captured from the live pages, so a redesign shows
up as a failing test rather than as an empty list in the UI. The bulk endpoint
is tested mostly for what it will NOT do -- it is an endpoint that turns a JSON
list into hundreds of scrapes, so every guard on it matters more than the happy
path.
"""

import pytest

from mediaforge import catalogue


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


def test_bulk_refuses_urls_that_are_not_in_the_catalogue(as_user, monkeypatch):
    """The catalogue is the closed set of things this page may act on. Without
    this check the endpoint would scrape any url it is handed -- a request
    forgery tool with a queue attached."""
    from mediaforge.web.routes import catalogue as routes

    # Validated against the STORED lists now (web/catalogue_store), not a live
    # fetch: a POST must not be able to trigger two multi-megabyte downloads
    # just to answer "is this url in a catalogue".
    monkeypatch.setattr(routes.catalogue_store, "all_entries", lambda: {
        "aniworld": [{"title": "A", "url": "https://aniworld.to/anime/stream/a", "alt": ""}]})
    resp = as_user("admin").post("/api/catalogue/bulk", json={
        "urls": ["https://evil.invalid/x", "https://aniworld.to/anime/stream/not-listed"],
    })
    assert resp.status_code == 400
    assert "no known urls" in resp.get_json()["error"]


def test_there_is_no_selection_ceiling(as_user, monkeypatch):
    """Marking the whole catalogue is a legitimate thing to want. The cost of a
    huge selection is handled where it belongs -- one series at a time in a
    background job that can be stopped -- not by refusing the request."""
    from mediaforge.web.routes import catalogue as routes

    assert not hasattr(routes, "MAX_BULK_SELECTION")

    fake = [{"title": str(i), "url": "https://aniworld.to/anime/stream/s%d" % i, "alt": ""}
            for i in range(1200)]
    monkeypatch.setattr(routes.catalogue_store, "all_entries",
                        lambda: {"aniworld": fake})
    monkeypatch.setattr(routes, "all_catalogues", lambda: {"aniworld": {}})
    # Nothing is actually expanded: the worker is what would scrape.
    monkeypatch.setattr(routes.catalogue_worker, "start_job",
                        lambda *a, **k: {"id": "test", "total": len(a[1])})

    resp = as_user("admin").post("/api/catalogue/bulk",
                                 json={"urls": [e["url"] for e in fake]})
    assert resp.status_code == 202
    assert resp.get_json()["total"] == 1200


def test_a_selection_may_span_several_sources(as_user, monkeypatch):
    """The page shows one merged list, so there is no "the" source to check
    against -- each url is resolved against whichever catalogue holds it."""
    from mediaforge.web.routes import catalogue as routes

    per_source = {
        "aniworld": [{"title": "A", "url": "https://aniworld.to/anime/stream/a", "alt": ""}],
        "sto": [{"title": "B", "url": "https://serienstream.to/serie/b", "alt": ""}],
    }
    monkeypatch.setattr(routes, "all_catalogues", lambda: {k: {} for k in per_source})
    monkeypatch.setattr(routes.catalogue_store, "all_entries", lambda: per_source)
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
                 'cat-modal-backdrop',      # details modal header image
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
