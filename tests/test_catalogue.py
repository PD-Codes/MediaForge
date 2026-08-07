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
    monkeypatch.setattr(catalogue, "get_catalogue",
                        lambda sid, force=False: [
                            {"title": "A", "url": "https://aniworld.to/anime/stream/a", "alt": ""}])
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
    monkeypatch.setattr(catalogue, "get_catalogue", lambda sid, force=False: fake)
    monkeypatch.setattr(routes, "get_catalogue", lambda sid, force=False: fake)
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
    monkeypatch.setattr(routes, "get_catalogue",
                        lambda sid, force=False: per_source.get(sid))
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
