"""UpTime monitor: probe verdicts, bucketing, defaults and the round lock.

Each test here pins one of the bugs the July 2026 audit found, so a
regression shows up as a named failure rather than as a wrong number on a
dashboard nobody stares at.
"""

import threading
import time

import pytest


# ── Probe verdict ────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status_code, headers=None, text="", url=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.url = url


class _FakeSession:
    """Stands in for config.GLOBAL_SESSION; records the calls it received."""

    def __init__(self, head=None, get=None):
        self._head, self._get = head, get
        self.calls = []

    def head(self, url, **kw):
        self.calls.append(("head", url))
        if self._head is None:
            raise AssertionError("unexpected HEAD")
        return self._head

    def get(self, url, **kw):
        self.calls.append(("get", url))
        if self._get is None:
            raise AssertionError("unexpected GET")
        return self._get


@pytest.fixture()
def probe(monkeypatch):
    """_probe_site with the network replaced by a scripted session."""
    from mediaforge import config
    from mediaforge.web import uptime_monitor

    def _run(head=None, get=None, **kw):
        sess = _FakeSession(head=head, get=get)
        monkeypatch.setattr(config, "GLOBAL_SESSION", sess, raising=False)
        entry = uptime_monitor._probe_site(
            "https://example.to", "example.to", ["example"],
            expected_headers={"server": "cloudflare"},
            timeout=1, resolve_ip=False, **kw,
        )
        entry["_calls"] = sess.calls
        return entry

    return _run


def test_probe_verifies_a_real_200(probe):
    r = probe(head=_FakeResp(200, {"server": "cloudflare"}, url="https://example.to/"))
    assert r["site_verified"] is True
    assert r["_calls"] == [("head", "https://example.to")]  # HEAD only


def test_probe_does_not_trust_a_cdn_block_page(probe):
    """A 403 carrying the right CDN signature must NOT count as up.

    This is the bug: Cloudflare's own block/challenge page answers with
    ``server: cloudflare``, so a header-only verdict reported a blocked site
    as online -- the exact event the monitor exists to catch.
    """
    r = probe(
        head=_FakeResp(403, {"server": "cloudflare"}, url="https://example.to/"),
        get=_FakeResp(403, {"server": "cloudflare"},
                      text="<html>Attention Required! Cloudflare Ray ID</html>",
                      url="https://example.to/"),
    )
    assert r["headers_matched"] is True
    assert r["status_ok"] is False
    assert r["site_verified"] is False
    assert ("get", "https://example.to") in r["_calls"]  # body fallback ran


def test_probe_detects_an_isp_block_page(probe):
    r = probe(
        head=_FakeResp(200, {"server": "nginx"}, url="https://example.to/"),
        get=_FakeResp(200, {"server": "nginx"},
                      text="Der Zugang zu der von Ihnen aufgerufenen Website "
                           "wurde gesperrt (CUII.info)",
                      url="https://example.to/"),
    )
    assert r["blocked"] is True
    assert r["site_verified"] is False


def test_probe_accepts_a_404_that_carries_a_real_body_marker(probe):
    """A genuine site may answer 404 on "/" -- a body marker still verifies it,
    but being on the right domain alone must not (a block page is too)."""
    r = probe(
        head=_FakeResp(404, {"server": "cloudflare"}, url="https://example.to/"),
        get=_FakeResp(404, {"server": "cloudflare"},
                      text="<title>example — page not found</title>",
                      url="https://example.to/"),
    )
    assert r["site_verified"] is True

    r2 = probe(
        head=_FakeResp(404, {"server": "cloudflare"}, url="https://example.to/"),
        get=_FakeResp(404, {"server": "cloudflare"}, text="nothing here",
                      url="https://example.to/"),
    )
    assert r2["site_verified"] is False


def test_probe_skips_dns_when_not_asked(probe, monkeypatch):
    from mediaforge.web import uptime_monitor

    def _boom(*a, **kw):
        raise AssertionError("_resolve_ip must not run for the monitor")

    monkeypatch.setattr(uptime_monitor, "_resolve_ip", _boom)
    r = probe(head=_FakeResp(200, {"server": "cloudflare"}, url="https://example.to/"))
    assert r["ip"] is None


# ── Source defaults (the "hanime" hardcoding) ────────────────────────────────
def test_adult_source_is_opt_in_everywhere_via_one_rule(app):
    from mediaforge.web import source_policy, uptime_monitor

    with app.app_context():
        assert source_policy.source_enabled_default("hanime") == "0"
        assert source_policy.source_enabled_default("aniworld") == "1"
        # The UpTime tracking default must come from the same rule, not from
        # its own copy of the id.
        assert uptime_monitor._tracked_default("hanime") == "0"
        assert uptime_monitor._tracked_default("aniworld") == "1"


def test_setting_is_on_tolerates_word_forms():
    from mediaforge.web.source_policy import setting_is_on

    assert setting_is_on("1") and setting_is_on("true") and setting_is_on("on")
    assert not setting_is_on("0")
    assert not setting_is_on("")
    assert not setting_is_on(None)


def test_third_party_site_keeps_its_own_defaults(app):
    from mediaforge.web import uptime_monitor as um

    with app.app_context():
        um.register_monitor_site(
            "test_mod", "testsite", "TestSite", "https://test.invalid",
            "test.invalid", body_markers=["test"],
            enabled_setting_key="testsite_search_enabled",
            tracked_by_default=False,
        )
        try:
            assert um._tracked_default("testsite") == "0"
            # An unset module key defaults to enabled: the module was
            # installed on purpose, and guessing "off" showed a permanent
            # "source disabled" badge.
            assert um._MONITOR_ENABLED_DEFAULTS["testsite"] == "1"
            assert um._uptime_config()["tracked"]["testsite"] is False
        finally:
            um.unregister_monitor_site("test_mod")
        assert "testsite" not in um._MONITOR_SITES
        assert "testsite" not in um._MONITOR_TRACKED_DEFAULTS


def test_registering_a_site_mid_round_does_not_kill_the_round(app, monkeypatch):
    """_MONITOR_SITES is mutated from the request thread; the round iterates a
    snapshot so it cannot raise "dictionary changed size during iteration"."""
    from mediaforge.web import uptime_monitor as um

    seen = []

    def _fake_probe(url, *a, **kw):
        seen.append(url)
        if len(seen) == 1:  # mutate the live dict mid-iteration
            um._MONITOR_SITES["injected"] = ("X", "https://x.invalid", "x.invalid", ["x"], {})
        return {"http_ok": True, "site_verified": True, "response_ms": 1, "http_status": 200}

    with app.app_context():
        monkeypatch.setattr(um, "_probe_site", _fake_probe)
        monkeypatch.setattr(um, "record_uptime_heartbeat", lambda *a, **kw: None)
        monkeypatch.setattr(um, "prune_uptime_heartbeats", lambda *a, **kw: None)
        cfg = um._uptime_config()
        cfg["tracked"] = {k: True for k in um._MONITOR_SITES}
        try:
            um._uptime_run_round(cfg)  # must not raise
        finally:
            um._MONITOR_SITES.pop("injected", None)
        assert seen


def test_only_one_round_runs_at_a_time(app, monkeypatch):
    from mediaforge.web import uptime_monitor as um

    started = threading.Event()
    release = threading.Event()
    rounds = []

    def _slow_probe(url, *a, **kw):
        rounds.append(url)
        started.set()
        release.wait(2)
        return {"http_ok": True, "site_verified": True}

    with app.app_context():
        monkeypatch.setattr(um, "_probe_site", _slow_probe)
        monkeypatch.setattr(um, "record_uptime_heartbeat", lambda *a, **kw: None)
        monkeypatch.setattr(um, "prune_uptime_heartbeats", lambda *a, **kw: None)
        cfg = um._uptime_config()
        cfg["tracked"] = {k: True for k in um._MONITOR_SITES}

        th = threading.Thread(target=um._uptime_run_round, args=(cfg,), daemon=True)
        th.start()
        assert started.wait(2)
        assert um.uptime_round_in_progress() is True
        before = len(rounds)
        um._uptime_run_round(cfg)  # second round: must return without probing
        assert len(rounds) == before
        release.set()
        th.join(5)
        assert um.uptime_round_in_progress() is False


# ── Bucketing and statistics ─────────────────────────────────────────────────
@pytest.mark.parametrize("span", [604800, 3601, 299, 3600])
def test_buckets_are_even_and_contain_their_own_checks(app, span):
    """Every heartbeat must fall inside the bar that counts it, and no bar may
    be wider than the others by more than a rounding second."""
    from mediaforge.web import db

    n = 50
    start = 1_700_000_000
    with app.app_context():
        db.prune_uptime_heartbeats(0)
        src = "buckettest%d" % span
        for i in range(n):
            db.record_uptime_heartbeat(src, "up", response_ms=10,
                                       ts=start + (i * span) // n)
        rr = db.get_uptime_range(src, start, start + span, n_buckets=n)

    widths = [b["end"] - b["start"] for b in rr["buckets"]]
    assert max(widths) - min(widths) <= 1
    assert sum(b["total"] for b in rr["buckets"]) == n
    for b in rr["buckets"]:
        assert b["start"] <= b["end"]


def test_uptime_pct_counts_degraded_as_not_up(app):
    """The failure-threshold debounce records the first rounds of a real
    outage as 'degraded'. Counting those as uptime reported every outage
    shorter than it was."""
    from mediaforge.web import db

    now = int(time.time())
    with app.app_context():
        src = "pcttest"
        for i in range(6):
            db.record_uptime_heartbeat(src, "up", response_ms=10, ts=now - 600 + i)
        for i in range(2):
            db.record_uptime_heartbeat(src, "degraded", ts=now - 500 + i)
        for i in range(2):
            db.record_uptime_heartbeat(src, "down", ts=now - 400 + i)
        rr = db.get_uptime_range(src, now - 3600, now + 1, n_buckets=10)

    st = rr["stats"]
    assert st["total"] == 10
    assert st["up_count"] == 6
    assert st["degraded_count"] == 2
    assert st["down_count"] == 2
    assert st["uptime_pct"] == 60.0
