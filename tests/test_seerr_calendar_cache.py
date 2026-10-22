"""Seerr request lookups are cached, and a failure keeps the last good answer."""

from mediaforge.web.routes import calendar_routes as cr


def _setup(monkeypatch, results):
    calls = []
    clock = [1000.0]
    settings = {"seerr_url": "http://seerr", "seerr_api_key": "k"}
    monkeypatch.setattr(cr, "get_setting", lambda k, d="": settings.get(k, d))
    monkeypatch.setattr(cr.time, "monotonic", lambda: clock[0])

    def fetch(url, key):
        calls.append(1)
        return results.pop(0)

    monkeypatch.setattr(cr, "_fetch_seerr_requested_media", fetch)
    cr._seerr_cache.update({"key": None, "at": 0.0, "out": []})
    return calls, clock


def test_second_call_is_served_from_cache(monkeypatch):
    item = [{"tmdb_id": 1, "media_type": "tv"}]
    calls, clock = _setup(monkeypatch, [(item, True), ([], True)])
    assert cr._seerr_requested_media() == item
    assert cr._seerr_requested_media() == item
    assert len(calls) == 1
    clock[0] += cr._SEERR_TTL + 1
    assert cr._seerr_requested_media() == []
    assert len(calls) == 2


def test_failure_keeps_last_good_answer_and_backs_off(monkeypatch):
    item = [{"tmdb_id": 1, "media_type": "tv"}]
    calls, clock = _setup(monkeypatch, [(item, True), ([], False), (item, True)])
    cr._seerr_requested_media()
    clock[0] += cr._SEERR_TTL + 1
    assert cr._seerr_requested_media() == item  # fetch failed, old data stays
    assert cr._seerr_requested_media() == item
    assert len(calls) == 2  # no retry inside the backoff window
    clock[0] += cr._SEERR_RETRY + 1
    cr._seerr_requested_media()
    assert len(calls) == 3
