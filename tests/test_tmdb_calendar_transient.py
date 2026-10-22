"""A TMDB timeout / 5xx must not be cached as the calendar answer."""

import pytest

from mediaforge.web import tmdb_cache as tc


class _HttpErr(Exception):
    def __init__(self, code):
        self.response = type("R", (), {"status_code": code})()


def test_error_classification():
    assert tc._is_transient_tmdb_error(_HttpErr(503))
    assert tc._is_transient_tmdb_error(_HttpErr(429))
    assert tc._is_transient_tmdb_error(TimeoutError())  # no response at all
    assert not tc._is_transient_tmdb_error(_HttpErr(404))


@pytest.fixture()
def cal(monkeypatch):
    stored = []
    monkeypatch.setattr(tc, "get_tmdb_cache", lambda *a, **k: None)
    monkeypatch.setattr(tc, "set_tmdb_cache", lambda k, v: stored.append(k))
    monkeypatch.setattr(tc._tmdb_rl, "acquire", lambda *a, **k: None)
    tc._tmdb_cal_backoff.clear()
    return stored


def test_transient_failure_is_not_cached_and_backs_off(cal, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise TimeoutError()

    monkeypatch.setattr(tc._rq_tmdb, "get", boom)
    assert tc._tmdb_calendar_episodes(1, "key")["episodes"] == []
    assert tc._tmdb_calendar_episodes(1, "key")["episodes"] == []
    assert cal == [] and len(calls) == 1  # 2nd call inside the retry window


def test_404_is_a_real_answer_and_gets_cached(cal, monkeypatch):
    class Resp:
        def raise_for_status(self):
            raise _HttpErr(404)

    monkeypatch.setattr(tc._rq_tmdb, "get", lambda *a, **k: Resp())
    tc._tmdb_calendar_episodes(2, "key")
    assert len(cal) == 1
