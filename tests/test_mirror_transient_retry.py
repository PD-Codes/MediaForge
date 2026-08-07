"""A single transient 502 must not fail a source that has only one host.

`request_with_failover` answers a "site is not here" status by moving to the
next mirror -- which is right, except that half the sources have exactly one
known host (filmo.to, 9anime.or.at, aniwaves.ru, filmpalast.to, hanime.tv).
For those there is no next mirror, so one hiccup from the origin failed the
whole request; aniwaves.ru answering 502 once was enough to fail a download
attempt outright.
"""

import pytest

from mediaforge import mirrors


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code
        self.url = "https://aniwaves.ru/watch/1"
        self.headers = {}


class _Session:
    """Answers each request from a scripted list of statuses."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return _Resp(self.statuses.pop(0) if self.statuses else 200)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(mirrors.time, "sleep", lambda *_: None)


SINGLE_HOST_URL = "https://aniwaves.ru/watch/82736"


def test_a_transient_502_on_the_only_host_is_retried_once():
    session = _Session([502, 200])
    resp = mirrors.request_with_failover(session, "GET", SINGLE_HOST_URL)
    assert resp.status_code == 200
    assert len(session.calls) == 2


def test_a_second_failure_is_reported_not_retried_forever():
    session = _Session([502, 502])
    resp = mirrors.request_with_failover(session, "GET", SINGLE_HOST_URL)
    assert resp.status_code == 502
    assert len(session.calls) == 2


def test_a_post_is_never_retried():
    """filmo.to's /n mints a one-shot token -- repeating the POST burns it."""
    session = _Session([502, 200])
    resp = mirrors.request_with_failover(session, "POST", "https://filmo.to/n")
    assert resp.status_code == 502
    assert len(session.calls) == 1


def test_a_permanent_status_is_not_retried():
    """403 is an answer about this host, not a hiccup; retrying it just adds
    a second, identical wait."""
    session = _Session([403, 200])
    resp = mirrors.request_with_failover(session, "GET", SINGLE_HOST_URL)
    assert resp.status_code == 403
    assert len(session.calls) == 1


def test_the_uptime_probe_still_sees_the_truth():
    """The monitor exists to report what the site answered. A retried
    best-of-two would quietly hide exactly the outages it watches for."""
    session = _Session([502, 200])
    resp = mirrors.request_with_failover(session, "GET", SINGLE_HOST_URL, probe=True)
    assert resp.status_code == 502
    assert len(session.calls) == 1
