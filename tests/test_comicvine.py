"""ComicVine enrichment: the four ways it is allowed to hurt someone.

The lookup itself is a nice-to-have -- a publisher name on a comic card. The
things guarded here are not:

1. **No key must mean silence.** Not an exception, not an ERROR log (which
   telemetry/hooks.py would turn into a crash report), not a request.
2. **The throttle must hold.** ComicVine allows 200 requests per resource and
   hour and locks the key out on violation. An unthrottled scan of a
   3000-issue library bans the user's key, so the cap is asserted here rather
   than trusted.
3. **A miss must be cached.** Most files in a real comic library are not on
   ComicVine. If a miss were re-asked every scan, the budget from (2) would be
   spent entirely on questions that already have an answer.
4. **The key must never reach a log.** It travels as a query parameter, and
   HTTP exception messages routinely quote the full request URL.

Nothing here touches the network: the session is replaced wholesale.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediaforge.web import comicvine_service as cv  # noqa: E402


API_KEY = "cv-secret-key-do-not-log-1234567890"


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    """Stands in for config.GLOBAL_SESSION. Records every call it gets."""

    def __init__(self, payload=None, status_code=200, raises=None):
        self.payload = payload if payload is not None else _ok([])
        self.status_code = status_code
        self.raises = raises
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        if self.raises is not None:
            raise self.raises
        return _FakeResponse(self.payload, self.status_code)


def _ok(results, total=None):
    return {"error": "OK", "status_code": 1, "results": results,
            "number_of_total_results": total if total is not None else len(results)}


VOLUME = {
    "id": 4050,
    "name": "Saga",
    "start_year": "2012",
    "publisher": {"name": "Image"},
    "image": {"super_url": "https://comicvine.gamespot.com/a/uploads/saga.jpg"},
    "deck": "A soldier and a soldier.",
}

ISSUE = {
    "id": 9001,
    "name": "Chapter One",
    "issue_number": "1",
    "cover_date": "2012-03-14",
    "image": {"super_url": "https://comicvine.gamespot.com/a/uploads/saga-1.jpg"},
    "deck": "The one where it starts.",
    "character_credits": [{"name": "Alana"}, {"name": "Marko"}, {"name": None}],
}


@pytest.fixture
def cvmod(app, monkeypatch):
    """A clean ComicVine module: key set, integration on, caches empty.

    Returns a helper that installs a fake session and hands it back.
    """
    from mediaforge.web import db

    with app.app_context():
        db.set_setting(cv.SETTING_API_KEY, API_KEY)
        db.set_setting(cv.SETTING_ENABLED, "1")
        db.set_setting(cv.SETTING_COOLDOWN, "")
        db.set_setting(cv.SETTING_RATE_WINDOW, "")
    cv.invalidate_cache()
    cv._rate.reset()

    def install(**kwargs):
        session = _FakeSession(**kwargs)
        monkeypatch.setattr(cv, "_session", lambda: session)
        return session

    yield install

    cv.invalidate_cache()
    cv._rate.reset()
    with app.app_context():
        db.set_setting(cv.SETTING_API_KEY, "")
        db.set_setting(cv.SETTING_ENABLED, "0")
        db.set_setting(cv.SETTING_COOLDOWN, "")
        db.set_setting(cv.SETTING_RATE_WINDOW, "")


# ---------------------------------------------------------------------------
# 1. No key / disabled
# ---------------------------------------------------------------------------

def test_without_a_key_nothing_happens(app, cvmod, caplog):
    from mediaforge.web import db
    session = cvmod()
    with app.app_context():
        db.set_setting(cv.SETTING_API_KEY, "")
    with caplog.at_level(logging.DEBUG):
        assert cv.enrich("Saga", "1") == {}
        assert cv.search_volume("Saga") is None
        assert cv.search_issue(4050, "1") is None
    assert session.calls == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_disabled_integration_makes_no_request(app, cvmod):
    from mediaforge.web import db
    session = cvmod(payload=_ok([VOLUME]))
    with app.app_context():
        db.set_setting(cv.SETTING_ENABLED, "0")
    assert cv.enrich("Saga", "1") == {}
    assert session.calls == []


def test_offline_returns_empty_without_raising(cvmod):
    cvmod(raises=OSError("Max retries exceeded"))
    assert cv.enrich("Saga", "1") == {}


def test_test_connection_without_a_key_is_a_plain_error(app, cvmod):
    from mediaforge.web import db
    session = cvmod()
    with app.app_context():
        db.set_setting(cv.SETTING_API_KEY, "")
    assert cv.test_connection() == {"ok": False, "error": "no_api_key"}
    assert session.calls == []


# ---------------------------------------------------------------------------
# 2. Throttle
# ---------------------------------------------------------------------------

def test_throttle_caps_requests_per_resource(cvmod, monkeypatch):
    session = cvmod(payload=_ok([]))
    monkeypatch.setattr(cv, "_rate", cv._RateLimiter(limit=2))

    for i in range(5):
        cv.search_volume(f"Series {i}")

    assert len(session.calls) == 2, "the throttle let more than the cap through"


def test_throttle_counts_resources_separately(cvmod, monkeypatch):
    session = cvmod(payload=_ok([VOLUME]))
    monkeypatch.setattr(cv, "_rate", cv._RateLimiter(limit=1))

    assert cv.search_volume("Saga") is not None      # spends the volumes budget
    assert cv.search_issue(4050, "1") is not None    # issues has its own
    assert len(session.calls) == 2
    assert cv.search_volume("Other") is None         # volumes budget is gone
    assert len(session.calls) == 2


def test_throttle_survives_a_restart(app, cvmod, monkeypatch):
    """A fresh process must not hand out a fresh hourly budget."""
    cvmod(payload=_ok([]))
    monkeypatch.setattr(cv, "_rate", cv._RateLimiter(limit=3))
    cv.search_volume("Saga")
    cv.search_volume("Bone")

    restarted = cv._RateLimiter(limit=3)   # same state a new process would load
    assert restarted.remaining("volumes") == 1


def test_a_reported_rate_limit_stops_everything(app, cvmod):
    """ComicVine answering 107 means the local count and the server's
    disagree -- the server wins, and nothing is sent for a full window."""
    session = cvmod(payload={"error": "rate limit exceeded", "status_code": 107,
                             "results": []})
    assert cv.search_volume("Saga") is None
    assert len(session.calls) == 1
    assert cv._cooldown_left() > 0

    assert cv.search_volume("Bone") is None
    assert len(session.calls) == 1, "kept sending after ComicVine said stop"


def test_http_429_also_starts_the_cooldown(cvmod):
    session = cvmod(payload={}, status_code=429)
    assert cv.search_volume("Saga") is None
    assert cv._cooldown_left() > 0
    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# 3. Caching (a miss is cached too)
# ---------------------------------------------------------------------------

def test_a_miss_is_cached(cvmod):
    session = cvmod(payload=_ok([]))
    assert cv.search_volume("Totally Unknown Indie Zine") is None
    assert cv.search_volume("Totally Unknown Indie Zine") is None
    assert len(session.calls) == 1, "an unknown series was looked up twice"


def test_a_hit_is_cached(cvmod):
    session = cvmod(payload=_ok([VOLUME]))
    first = cv.search_volume("Saga")
    second = cv.search_volume("Saga")
    assert first == second
    assert len(session.calls) == 1


def test_a_throttled_lookup_is_not_cached_as_a_miss(cvmod, monkeypatch):
    """A temporary "no budget" must not freeze into a 24 h miss."""
    session = cvmod(payload=_ok([VOLUME]))
    monkeypatch.setattr(cv, "_rate", cv._RateLimiter(limit=0))
    assert cv.search_volume("Saga") is None
    assert session.calls == []

    monkeypatch.setattr(cv, "_rate", cv._RateLimiter(limit=5))
    assert cv.search_volume("Saga") is not None
    assert len(session.calls) == 1


def test_an_outage_is_not_cached_as_a_miss(cvmod, monkeypatch):
    session = cvmod(raises=OSError("connection reset"))
    assert cv.search_volume("Saga") is None

    ok_session = _FakeSession(payload=_ok([VOLUME]))
    monkeypatch.setattr(cv, "_session", lambda: ok_session)
    assert cv.search_volume("Saga") is not None


# ---------------------------------------------------------------------------
# 4. The key never reaches a log
# ---------------------------------------------------------------------------

def test_the_api_key_is_never_logged(cvmod, caplog):
    """The most likely leak: an HTTP error message quoting the request URL."""
    leaky = OSError(
        "HTTPSConnectionPool: /api/volumes/?api_key=" + API_KEY + "&format=json"
    )
    cvmod(raises=leaky)
    with caplog.at_level(logging.DEBUG):
        assert cv.enrich("Saga", "1") == {}
        assert cv.search_volume("Saga") is None
        assert cv.test_connection()["ok"] is False
    assert API_KEY not in caplog.text
    assert "api_key=" not in caplog.text or "api_key=***" in caplog.text


def test_nothing_logs_at_error_level(cvmod, caplog):
    """An ERROR record on the shared logger becomes a telemetry crash report."""
    cvmod(raises=OSError("boom"))
    with caplog.at_level(logging.DEBUG):
        cv.enrich("Saga", "1")
        cv.search_volume("Saga")
        cv.search_issue(4050, "1")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_redact_removes_the_key_from_any_text():
    text = "GET https://comicvine.gamespot.com/api/issues/?api_key=" + API_KEY
    assert API_KEY not in cv._redact(text, API_KEY)
    # Also without knowing the key -- the query parameter is stripped by shape.
    assert "abc123" not in cv._redact("?api_key=abc123&format=json")


# ---------------------------------------------------------------------------
# What enrich() actually returns
# ---------------------------------------------------------------------------

def test_enrich_returns_only_supplementary_fields(cvmod, monkeypatch):
    class _TwoStep(_FakeSession):
        """Answers /volumes/ and /issues/ from their own fixtures."""

        def get(self, url, params=None, timeout=None, headers=None):
            self.payload = _ok([ISSUE]) if "/issues/" in url else _ok([VOLUME])
            return super().get(url, params=params, timeout=timeout, headers=headers)

    session = _TwoStep()
    monkeypatch.setattr(cv, "_session", lambda: session)
    out = cv.enrich("Saga", "1", "2012")

    assert out["publisher"] == "Image"
    assert out["story_title"] == "Chapter One"
    assert out["summary"] == "The one where it starts."
    assert out["year"] == "2012"
    assert out["characters"] == ["Alana", "Marko"]
    assert out["cover_url"].startswith("https://comicvine.gamespot.com/")
    assert set(out) <= {"publisher", "summary", "cover_url", "year",
                        "story_title", "characters"}


def test_a_foreign_cover_host_is_dropped(cvmod):
    hostile = dict(VOLUME, image={"super_url": "https://evil.example/pixel.gif"})
    cvmod(payload=_ok([hostile]))
    assert cv.search_volume("Saga")["cover_url"] == ""


def test_garbage_fields_do_not_crash_the_parser(cvmod):
    junk = {"id": 1, "name": "Saga", "start_year": None, "publisher": "Image",
            "image": ["nope"], "deck": 42, "description": None}
    cvmod(payload=_ok([junk]))
    out = cv.search_volume("Saga")
    assert out["publisher"] == "" and out["cover_url"] == "" and out["year"] == ""


def test_a_completely_different_series_is_a_miss(cvmod):
    cvmod(payload=_ok([dict(VOLUME, name="Something Else Entirely")]))
    assert cv.search_volume("Saga") is None


# ---------------------------------------------------------------------------
# The settings endpoint
# ---------------------------------------------------------------------------

def test_the_endpoint_never_returns_the_key(app, as_user):
    from mediaforge.web import db
    with app.app_context():
        db.set_setting(cv.SETTING_API_KEY, "plaintext-comicvine-key-xyz")
    resp = as_user("admin").get("/api/settings/comicvine")
    assert resp.status_code == 200
    assert "plaintext-comicvine-key-xyz" not in resp.get_data(as_text=True)
    assert resp.get_json()["has_api_key"] is True
    with app.app_context():
        db.set_setting(cv.SETTING_API_KEY, "")


def test_saving_without_a_key_keeps_the_stored_one(app, as_user):
    from mediaforge.web import db
    client = as_user("admin")
    with app.app_context():
        db.set_setting(cv.SETTING_API_KEY, "keep-me")
    client.put("/api/settings/comicvine", json={"enabled": "1"})
    with app.app_context():
        assert db.get_setting(cv.SETTING_API_KEY, "") == "keep-me"
        assert db.get_setting(cv.SETTING_ENABLED, "") == "1"
        db.set_setting(cv.SETTING_API_KEY, "")
        db.set_setting(cv.SETTING_ENABLED, "0")


def test_the_key_is_stored_encrypted(app):
    """It is in db.SENSITIVE_KEYS, so the raw row must not be readable."""
    from mediaforge.web import db
    with app.app_context():
        assert db.is_sensitive_key(cv.SETTING_API_KEY)
        db.set_setting(cv.SETTING_API_KEY, "encrypt-me-please")
        conn = db.get_db()
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?",
                           (cv.SETTING_API_KEY,)).fetchone()
        assert "encrypt-me-please" not in str(row["value"])
        assert db.get_setting(cv.SETTING_API_KEY, "") == "encrypt-me-please"
        db.set_setting(cv.SETTING_API_KEY, "")
