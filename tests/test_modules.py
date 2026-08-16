"""Third-party modules: the Module Manager page, the seams a module may
use, the store's trust-store guard and the shared TMDB helper.

Merged from: test_extensions_page.py, test_core_module_seams.py, test_truststore_guard.py, test_tmdb_lookup_media.py.
"""

import pytest
import ssl


# ==========================================================================
# test_extensions_page.py
#
# The Module Manager page is a contract, not just a layout.
# 
# The page was reworked into three views (Installed / Store / Settings) with the
# store as a split browser. Everything the *rest* of the app reaches into it with
# survives that rework only as long as the hooks below keep existing:
# 
#   * ``module_store.js`` renders into #extStoreList / #extStoreDetail / #extStoreRail
#     and switches views through [data-extview]; it also hangs the "Update" pill on
#     an installed card found by ``data-module-id``.
#   * ``extension_cards.js`` drives the Modules / Theme Packs tabs through
#     [data-exttab] and the state filters through #mmFilters / [data-mm-filter],
#     matched against each card's ``data-mm-state``.
#   * Settings → Design links here with ``#store``, which only resolves to a view
#     because a [data-extview="store"] entry exists.
#   * Every module's enable switch is the shared ``.thirdparty-toggle``, and every
#     folder — healthy or not — keeps an ``.ext-uninstall-btn``.
# 
# A rename or a tidy-up on any of those turns into a dead button in the browser
# and nothing else, which is exactly the kind of breakage a page test is for.
# ==========================================================================
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


# ==========================================================================
# test_core_module_seams.py
#
# The three seams a module is allowed to use instead of introspecting the core.
# 
# Each of these replaces a trick modules were doing because the core offered no
# contract: unwrapping login_required to reach a raw view, querying one source
# registry and assuming it was all of them, and mutating the parsed request body
# to hand a core view server-side values.
# ==========================================================================
# ── 1. raw views ─────────────────────────────────────────────────────────
def test_raw_views_are_published(app):
    raw = app.extensions.get("mediaforge_raw_views") or {}
    assert "api_download" in raw
    # The registered view is the wrapped one, the snapshot is not.
    assert raw["api_download"] is not app.view_functions["api_download"]


def test_raw_views_cover_every_endpoint(app):
    raw = app.extensions["mediaforge_raw_views"]
    assert set(app.view_functions) <= set(raw)


# ── 2. one query over all source registries ──────────────────────────────
def test_all_source_ids_includes_feed_only_sources(app):
    from mediaforge.home_feed import (register_home_feed_source,
                                      unregister_home_feed_source)
    from mediaforge.web.source_policy import all_source_ids, search_source_ids

    register_home_feed_source("test-seam-module", "seamsource", "Seam Source",
                              {"new": lambda: []})
    try:
        with app.app_context():
            assert "seamsource" not in search_source_ids()
            assert "seamsource" in all_source_ids()
            # Still a superset of the search catalogue.
            assert set(search_source_ids()) <= all_source_ids()
    finally:
        unregister_home_feed_source("test-seam-module")


# ── 3. internal callers pass a payload instead of faking a body ──────────
@pytest.mark.parametrize("payload,expected", [
    (None, "episodes"),      # body wins when no payload is given
    ({}, "series_url"),      # payload wins over the body
])
def test_api_download_payload_keyword(app, payload, expected):
    view = app.extensions["mediaforge_raw_views"]["api_download"]
    with app.test_request_context("/api/download", method="POST",
                                  json={"series_url": "https://example.invalid/s"}):
        kwargs = {} if payload is None else {"payload": payload}
        body, status = view(**kwargs)
        assert status == 400
        assert expected in body.get_json()["error"]


# ==========================================================================
# test_truststore_guard.py
#
# The OS trust store must never be able to take the module store down.
# 
# Background: truststore captures ``super(ssl.SSLContext, ssl.SSLContext)`` at
# import time. Normally that resolves to the C-level descriptor in
# ``_ssl._SSLContext`` and writing ``verify_mode`` never enters ``ssl.py`` at
# all. But if something else in the interpreter replaced ``ssl.SSLContext`` with
# a subclass first (a .pth file or sitecustomize.py, as TLS-inspection tooling
# installs), the same write lands in ``ssl.py``'s Python property, whose setter
# reads the module global ``SSLContext`` at call time -- now the subclass -- and
# calls itself until RecursionError. Every TLS handshake through truststore then
# dies, which reached users as "module store unreachable".
# 
# MediaForge cannot fix that interpreter. It must route around it.
# ==========================================================================
@pytest.fixture()
def fresh_config(monkeypatch):
    """config with its truststore verdict un-cached, restored afterwards."""
    from mediaforge import config

    monkeypatch.setattr(config, "_truststore_checked", False, raising=False)
    monkeypatch.setattr(config, "_TRUSTSTORE_UNSAFE_REASON", None, raising=False)
    return config


def test_a_healthy_interpreter_still_uses_the_os_trust_store(fresh_config):
    truststore = pytest.importorskip("truststore")

    assert fresh_config._truststore_is_safe() is True
    assert fresh_config.truststore_unsafe_reason() is None
    ctx = fresh_config.ssl_context_for("https://example.com/")
    assert isinstance(ctx, truststore.SSLContext)


def test_an_injected_ssl_module_falls_back_to_certifi(fresh_config, monkeypatch):
    """The failure this guards against, reproduced by injecting a subclass.

    ``ssl_context_for()`` returning None is not a downgrade of security: None
    means "Python's default context", i.e. the bundled certifi roots with
    verification fully on.
    """
    pytest.importorskip("truststore")

    class _Injected(ssl.SSLContext):
        pass

    monkeypatch.setattr(ssl, "SSLContext", _Injected)

    assert fresh_config._truststore_is_safe() is False
    reason = fresh_config.truststore_unsafe_reason()
    assert reason and "ssl.SSLContext has been replaced" in reason
    assert fresh_config.ssl_context_for("https://example.com/") is None


def test_the_verdict_is_cached(fresh_config, monkeypatch):
    """The check runs once; the warning must not be logged per request."""
    pytest.importorskip("truststore")

    calls = []
    real = fresh_config._truststore_is_safe

    fresh_config._truststore_is_safe()
    monkeypatch.setattr(ssl, "SSLContext", type("X", (ssl.SSLContext,), {}))
    # Already decided -- a later injection does not re-open the question, so a
    # long-running process keeps one consistent answer.
    assert real() is True
    assert not calls


def test_the_store_retries_without_truststore_on_recursion(monkeypatch):
    """Even if the pre-flight check misses a variant of the bug, one
    RecursionError must not cost the user the module store."""
    from mediaforge.web.thirdparties import store

    seen = []

    def _fake_urlopen_read(req, timeout, context, max_bytes):
        seen.append(context)
        if context is not None:
            raise RecursionError("maximum recursion depth exceeded")
        return b'{"modules": []}'

    monkeypatch.setattr(store, "_urlopen_read", _fake_urlopen_read)
    monkeypatch.setattr("mediaforge.config.ssl_context_for",
                        lambda url: object())

    data = store._http_get("https://example.com/store/index.json", 1024)
    assert data == b'{"modules": []}'
    assert len(seen) == 2 and seen[1] is None  # retried on the default context


def test_a_recursion_without_a_context_is_not_swallowed(monkeypatch):
    """If there is no truststore context to blame, the error is real and has
    to surface rather than be retried into an identical failure."""
    from mediaforge.web.thirdparties import store

    def _boom(req, timeout, context, max_bytes):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(store, "_urlopen_read", _boom)
    monkeypatch.setattr("mediaforge.config.ssl_context_for", lambda url: None)

    with pytest.raises(RecursionError):
        store._http_get("https://example.com/store/index.json", 1024)


# ==========================================================================
# test_tmdb_lookup_media.py
#
# lookup_media(): the public TMDB entry point filters what callers used to
# filter by hand.
# 
# Only routes/search.py ever checked title_confident, and calendar_routes.py
# open-coded ``found and media_type == "tv"`` in six places -- so a caller that
# forgot either guard rendered TMDB's first fuzzy /search/multi hit as a real one.
# ==========================================================================
@pytest.fixture
def tmdb(monkeypatch, app):
    """lookup_media with the network layer replaced and a key configured."""
    from mediaforge.web import tmdb_cache

    calls = []

    def fake_lookup(title, imdb_id, api_key, country, ui_lang="de"):
        calls.append((title, imdb_id, api_key, country, ui_lang))
        return fake_lookup.result

    fake_lookup.result = {"found": False}
    monkeypatch.setattr(tmdb_cache, "_tmdb_lookup_cached", fake_lookup)
    monkeypatch.setattr(
        tmdb_cache, "get_setting",
        lambda key, default=None: {
            "cineinfo_tmdb_api_key": "key123",
            "cineinfo_country": "DE",
        }.get(key, default),
    )
    return tmdb_cache, fake_lookup, calls


def test_returns_the_hit(tmdb):
    mod, fake, _calls = tmdb
    fake.result = {"found": True, "tmdb_id": 1, "media_type": "tv",
                   "title_confident": True}
    assert mod.lookup_media("Dark")["tmdb_id"] == 1


def test_miss_returns_none_not_a_found_false_dict(tmdb):
    mod, fake, _calls = tmdb
    fake.result = {"found": False}
    assert mod.lookup_media("Nope") is None


def test_media_type_mismatch_is_rejected(tmdb):
    mod, fake, _calls = tmdb
    fake.result = {"found": True, "tmdb_id": 1, "media_type": "movie",
                   "title_confident": True}
    assert mod.lookup_media("Dark", media_type="tv") is None
    assert mod.lookup_media("Dark", media_type="movie") is not None


def test_require_confident_rejects_a_fuzzy_match(tmdb):
    mod, fake, _calls = tmdb
    fake.result = {"found": True, "tmdb_id": 1, "media_type": "tv",
                   "title_confident": False}
    assert mod.lookup_media("Dark", require_confident=True) is None
    assert mod.lookup_media("Dark") is not None  # off by default


def test_no_api_key_returns_none_without_calling_tmdb(monkeypatch, app):
    from mediaforge.web import tmdb_cache
    monkeypatch.setattr(tmdb_cache, "get_setting", lambda key, default=None: "")

    def boom(*a, **kw):
        raise AssertionError("TMDB was called without a configured key")

    monkeypatch.setattr(tmdb_cache, "_tmdb_lookup_cached", boom)
    assert tmdb_cache.lookup_media("Dark") is None
    assert tmdb_cache.is_tmdb_configured() is False


def test_empty_query_returns_none_without_calling_tmdb(tmdb):
    mod, fake, calls = tmdb
    assert mod.lookup_media("") is None
    assert mod.lookup_media(None, imdb_id="  ") is None
    assert calls == []


def test_defaults_are_resolved_from_the_settings(tmdb):
    """A module passes a title and nothing else; the key/country/language come
    from the configuration, not from the caller."""
    mod, fake, calls = tmdb
    fake.result = {"found": True, "tmdb_id": 1, "media_type": "tv",
                   "title_confident": True}
    mod.lookup_media("Dark")
    assert calls[-1] == ("Dark", None, "key123", "DE", "de")


def test_explicit_api_key_skips_the_setting_read(tmdb):
    """Core loops pass a pre-resolved key so a lookup per title doesn't become
    a DB read per title."""
    mod, fake, calls = tmdb
    fake.result = {"found": True, "tmdb_id": 1, "media_type": "tv",
                   "title_confident": True}
    mod.lookup_media("Dark", api_key="other", country="US", ui_lang="en")
    assert calls[-1] == ("Dark", None, "other", "US", "en")
