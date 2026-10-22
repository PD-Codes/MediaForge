"""The Crunchyroll master switch is authoritative, and failures pause traffic.

Before: stored credentials alone kept the calendar/watchlist code talking to
Crunchyroll with the integration switched off, and every failed login was
retried immediately by every caller (one per poster card), which keeps a
Cloudflare block alive.
"""

import pytest

from mediaforge.web import crunchyroll_service as crs


class _Boom(Exception):
    pass


@pytest.fixture()
def svc(monkeypatch):
    settings = {"crunchyroll_enabled": "1", "crunchyroll_email": "a@b.de",
                "crunchyroll_password": "pw"}
    monkeypatch.setattr(crs, "get_setting", lambda k, d="": settings.get(k, d))
    monkeypatch.setattr(crs, "_IMPORT_OK", True)
    monkeypatch.setattr(crs, "clear_provider_cache", lambda *a, **k: None)
    monkeypatch.setattr(crs, "_report_crunchyroll_used", lambda: None)
    crs.invalidate_client()
    yield settings
    crs.invalidate_client()


def _count_builds(monkeypatch, exc=None, client=None):
    calls = []

    def build(*args, **kwargs):
        calls.append(1)
        if exc is not None:
            raise exc
        return client

    monkeypatch.setattr(crs, "_build_client", build)
    return calls


def test_master_switch_off_means_no_traffic(svc, monkeypatch):
    svc["crunchyroll_enabled"] = "0"
    calls = _count_builds(monkeypatch, client=object())
    assert crs.has_account() is False  # credentials alone are not enough
    assert crs.get_client() is None
    assert crs.get_watchlist_titles() == []
    assert crs.get_custom_list_entries() == []
    assert calls == []


def test_failed_login_pauses_retries_until_settings_change(svc, monkeypatch):
    calls = _count_builds(monkeypatch, exc=crs.CloudflareBlockError("blocked"))
    assert crs.get_client() is None
    assert crs.get_client() is None
    assert crs.get_client() is None
    assert len(calls) == 1  # the 2nd and 3rd call must not hit the network

    crs.invalidate_client()  # what saving the settings does
    assert crs.get_client() is None
    assert len(calls) == 2


def test_backoff_grows_and_is_capped(svc, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(crs.time, "time", lambda: now[0])
    for _ in range(30):
        crs._note_failure("cloudflare")
    assert crs._fail_until - now[0] == crs._FAIL_BACKOFF_MAX


def test_failed_lookup_is_not_cached_as_not_found(svc, monkeypatch):
    class Client:
        def search_series(self, *a, **k):
            raise crs.CloudflareBlockError("blocked")

    stored = []
    monkeypatch.setattr(crs, "get_provider_cache", lambda *a, **k: None)
    monkeypatch.setattr(crs, "set_provider_cache", lambda *a, **k: stored.append(a))
    monkeypatch.setattr(crs, "get_client", lambda: Client())
    assert crs.is_available("Frieren") is False
    assert stored == []  # a block is not "this title is not on Crunchyroll"
    assert crs._fail_until > 0  # ...and it pauses further requests


def test_impersonating_session_keeps_its_own_user_agent():
    from mediaforge.vendor.crunchyroll_api import Config, CrunchyrollClient

    class Sess:
        def __init__(self, impersonate):
            self.impersonate = impersonate
            self.headers = {}

    browser = Sess("chrome")
    CrunchyrollClient(Config(), http_session=browser)
    assert "User-Agent" not in browser.headers  # would clash with the TLS fingerprint

    plain = Sess(None)
    CrunchyrollClient(Config(), http_session=plain)
    assert "User-Agent" in plain.headers
