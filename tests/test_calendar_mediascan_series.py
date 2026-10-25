"""Calendar: the MediaScan ("media library") overlay.

MediaScan stores series as media_type 'show' while the calendar asked for
'tv', so the overlay was always empty. Title-only rows are now resolved cache
first, with live TMDB lookups only within the caller's budget (the watcher),
never inside the /api/calendar request.
"""

from mediaforge.web import db


def test_mediascan_series_are_found_and_title_rows_respect_the_budget(app, monkeypatch):
    from mediaforge.web.routes import calendar_routes as cal

    db.replace_mediascan_cache([
        {"tmdb_id": "123", "imdb_id": "", "tvdb_id": "", "title": "Has Id", "media_type": "show"},
        {"tmdb_id": "999", "imdb_id": "", "tvdb_id": "", "title": "A Movie", "media_type": "movie"},
        {"tmdb_id": "", "imdb_id": "", "tvdb_id": "", "title": "Title Only", "media_type": "show"},
    ])
    lookups = []

    def fake_lookup(title, **_kw):
        lookups.append(title)
        return {"found": True, "media_type": "tv", "tmdb_id": 456}

    monkeypatch.setattr(cal, "lookup_media", fake_lookup)
    monkeypatch.setattr(cal, "get_tmdb_cache_bulk_light", lambda keys: {})
    try:
        assert [r["title"] for r in db.get_mediascan_series()] == ["Has Id", "Title Only"]
        assert db.get_mediascan_ids_by_type("tv") == {"123"}

        # The request path: no live lookup at all.
        assert cal._mediascan_tv_ids("key", "DE", 0) == [123]
        assert lookups == []

        # The watcher: at most its budget.
        assert cal._mediascan_tv_ids("key", "DE", 1) == [123, 456]
        assert lookups == ["Title Only"]
    finally:
        db.clear_mediascan_cache()
