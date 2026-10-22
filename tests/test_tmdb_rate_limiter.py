"""The TMDB limiter must space concurrent callers out, not release them together."""

from mediaforge.web import tmdb_cache


def test_burst_is_spread_over_time(monkeypatch):
    waits = []
    monkeypatch.setattr(tmdb_cache.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(tmdb_cache.time, "sleep", waits.append)
    limiter = tmdb_cache._TmdbRateLimiter(rate=10.0)

    for _ in range(30):  # 10 free tokens, then 20 callers hitting an empty bucket
        limiter.acquire()

    queued = [w for w in waits if w > 0]
    assert len(queued) == 20
    # each later caller waits one slot (1/rate) longer than the one before
    assert queued == sorted(queued)
    assert abs(queued[-1] - 20 / 10.0) < 1e-6
