"""The three seams a module is allowed to use instead of introspecting the core.

Each of these replaces a trick modules were doing because the core offered no
contract: unwrapping login_required to reach a raw view, querying one source
registry and assuming it was all of them, and mutating the parsed request body
to hand a core view server-side values.
"""

import pytest


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
