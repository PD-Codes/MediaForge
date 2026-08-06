"""Error classification and settings profiles.

The classifier is worth testing precisely because it is a pile of substring
rules: the failure mode is not a crash, it is a confidently wrong explanation
that sends somebody to fix the wrong thing.
"""

import pytest

from mediaforge.web import error_explain as ee


@pytest.mark.parametrize("raw, cause", [
    ("OSError: [Errno 28] No space left on device", "disk_full"),
    ("Der ausgewählte Custom Path (ID #3) existiert nicht mehr", "path_missing"),
    ("PermissionError: [Errno 13] Permission denied: '/media/x.mkv'", "permission"),
    ("Captcha challenge was not solved in time", "captcha"),
    ("HTTP Error 429: Too Many Requests", "rate_limited"),
    ("HTTP Error 403: Forbidden", "blocked"),
    ("ERROR: unable to extract video url", "hoster_dead"),
    ("socket.gaierror: [Errno -2] Name or service not known", "dns"),
    ("SSLCertVerificationError: certificate verify failed", "tls"),
    ("ReadTimeout: HTTPSConnectionPool ... Read timed out", "timeout"),
    ("ConnectionResetError: [Errno 104] Connection reset by peer", "connection"),
    ("watchdog: download thread unresponsive", "watchdog_hang"),
    ("HTTP Error 503: Service Unavailable", "server_error"),
    ("HTTP Error 404: Not Found", "not_found"),
])
def test_known_errors_are_classified(raw, cause):
    assert ee.classify(raw) == cause


@pytest.mark.parametrize("raw, cause", [
    # The two that showed up as "Unbekannter Fehler" in the queue: every rule
    # used to be English-only while half the extractor catalogue is German.
    ("Keine VOE-Videoquelle auf der Seite gefunden.", "hoster_dead"),
    ("Nicht verfügbar in: Deutsch", "language_missing"),
    ("Nicht verfügbar in: Japanisch mit deutschen Untertiteln", "language_missing"),
    # The hoster name sits inside the compound noun, so a plain substring on
    # "keine videoquelle" misses.
    ("VeeV: Keine Videoquelle gefunden (https://x)", "hoster_dead"),
    ("VeeV: Keine CDN-URL gefunden (https://x)", "hoster_dead"),
    ("Vidoza: Video nicht verfügbar oder wurde entfernt.", "hoster_dead"),
    ("No Filemoon video source found in page.", "hoster_dead"),
    ("No redirect URL found in VOE response.", "hoster_dead"),
    ("Dieser VOE-Server ist derzeit im Wartungsmodus.", "hoster_maintenance"),
    ("get_direct_link_from_luluvdo is not implemented yet.", "not_implemented"),
    ("The provider 'x' is not yet implemented.", "not_implemented"),
    ("Host 'evil.internal' ist aus Sicherheitsgründen nicht erlaubt.", "security_blocked"),
    ("Datei nicht gefunden: /media/x.mkv", "path_missing"),
    ("no usable stream url", "hoster_dead"),
])
def test_german_and_provider_errors_are_classified(raw, cause):
    assert ee.classify(raw) == cause


def test_ffmpeg_exit_code_is_not_a_missing_binary():
    """A non-zero exit means bad input, not "download ffmpeg again"."""
    assert ee.classify("ffmpeg exited with code 1") == "ffmpeg_failed"
    assert ee.classify("ffmpeg: command not found") == "ffmpeg_missing"


def test_page_load_retries_keep_the_underlying_cause():
    """"…konnte nach N Versuchen nicht geladen werden: <err>" — <err> wins."""
    base = "VOE-Seite konnte nach 3 Versuchen nicht geladen werden: "
    assert ee.classify(base + "Read timed out") == "timeout"
    assert ee.classify(base + "certificate verify failed") == "tls"
    # No recognisable tail: still better than "unknown".
    assert ee.classify(base + "weird") == "connection"


def test_our_own_guard_is_not_reported_as_a_remote_block():
    """Sending somebody hunting for a VPN when we blocked it ourselves."""
    assert ee.classify("upstream resolved to a forbidden address") == "security_blocked"


def test_specific_rules_win_over_generic_ones():
    """"No space left on device" is an OSError too, and must not be one."""
    assert ee.classify("OSError: [Errno 28] No space left on device") == "disk_full"
    # A 403 mentioning a 500-ish number elsewhere must still be "blocked".
    assert ee.classify("HTTP Error 403: Forbidden (ref 502841)") == "blocked"


def test_unknown_stays_unknown():
    """A wrong explanation is worse than none."""
    result = ee.explain("Something entirely novel went sideways")
    assert result["cause"] == "unknown"
    assert result["raw"].startswith("Something entirely")


def test_every_cause_has_wording():
    """A rule pointing at a cause with no entry would render an empty box."""
    for cause, _matcher in ee._RULES:
        assert cause in ee.CAUSES, cause


def test_summarize_groups_and_counts():
    errors = [
        {"url": "e1", "error": "HTTP Error 403: Forbidden"},
        {"url": "e2", "error": "HTTP Error 403: Forbidden"},
        {"url": "e3", "error": "[Errno 28] No space left on device"},
    ]
    summary = ee.summarize(errors)
    assert summary["total"] == 3
    # Critical first, regardless of count.
    assert summary["causes"][0]["cause"] == "disk_full"
    blocked = [c for c in summary["causes"] if c["cause"] == "blocked"][0]
    assert blocked["count"] == 2


def test_summary_is_not_retryable_when_one_cause_is_not():
    """Offering "retry" on a disk-full job wastes the user's time twice."""
    assert ee.summarize([{"error": "HTTP Error 503"}])["retryable"] is True
    mixed = ee.summarize([
        {"error": "HTTP Error 503"},
        {"error": "No space left on device"},
    ])
    assert mixed["retryable"] is False


def test_examples_are_capped():
    errors = [{"error": "HTTP Error 503: x"} for _ in range(50)]
    bucket = ee.summarize(errors)["causes"][0]
    assert bucket["count"] == 50
    assert len(bucket["examples"]) == 3


# ---------------------------------------------------------------------------
# Settings profiles
# ---------------------------------------------------------------------------

def test_profile_never_exports_secrets(app):
    """The whole reason this exists as something you can paste into a ticket."""
    from mediaforge.web import settings_profile
    from mediaforge.web.db import SENSITIVE_KEYS, set_setting

    with app.app_context():
        set_setting("seerr_api_key", "super-secret-value")
        set_setting("naming_template", "{title} S{season}E{episode}")
        doc = settings_profile.export_profile("pytest")

    assert doc["settings"].get("naming_template")
    for key in SENSITIVE_KEYS:
        assert key not in doc["settings"], key
    assert "super-secret-value" not in str(doc)
    # The kids PIN is a secret that lives under an allowed prefix.
    assert "home_kids_pin" not in doc["settings"]


def test_profile_preview_reports_changes_without_writing(app):
    from mediaforge.web import settings_profile
    from mediaforge.web.db import get_setting, set_setting

    with app.app_context():
        set_setting("naming_template", "before")
        doc = {
            "format": "mediaforge-settings-profile",
            "format_version": 1,
            "name": "pytest",
            "settings": {"naming_template": "after"},
        }
        preview = settings_profile.preview_profile(doc)
        assert preview["ok"]
        assert preview["changes"] == [
            {"key": "naming_template", "from": "before", "to": "after"}]
        # Nothing written yet.
        assert get_setting("naming_template") == "before"

        settings_profile.import_profile(doc)
        assert get_setting("naming_template") == "after"


def test_profile_refuses_keys_outside_the_allowlist(app):
    from mediaforge.web import settings_profile
    from mediaforge.web.db import get_setting

    with app.app_context():
        doc = {
            "format": "mediaforge-settings-profile",
            "format_version": 1,
            "settings": {"seerr_api_key": "injected", "oidc_client_secret": "injected"},
        }
        result = settings_profile.import_profile(doc)
        assert result["applied"] == []
        assert set(result["refused"]) == {"seerr_api_key", "oidc_client_secret"}
        assert get_setting("seerr_api_key") != "injected"


def test_profile_rejects_foreign_documents(app):
    from mediaforge.web import settings_profile
    with app.app_context():
        assert settings_profile.preview_profile("not json")["error"] == "invalid_json"
        assert settings_profile.preview_profile({"format": "something-else"})["error"] == "not_a_profile"
        assert settings_profile.preview_profile(
            {"format": "mediaforge-settings-profile", "format_version": 99}
        )["error"] == "newer_format"


# ---------------------------------------------------------------------------
# Bulk queue actions
# ---------------------------------------------------------------------------

def test_bulk_rejects_bad_input(as_user):
    client = as_user("admin")
    assert client.post("/api/queue/bulk", json={"action": "nope", "ids": [1]}).status_code == 400
    assert client.post("/api/queue/bulk", json={"action": "cancel", "ids": []}).status_code == 400
    assert client.post("/api/queue/bulk",
                       json={"action": "cancel", "ids": list(range(600))}).status_code == 400


def test_bulk_reports_per_item_results(as_user):
    """One missing item must not fail the rest of the selection."""
    client = as_user("admin")
    resp = client.post("/api/queue/bulk",
                       json={"action": "remove", "ids": [999001, 999002]})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["succeeded"] == []
    assert set(body["failed"]) == {"999001", "999002"}
