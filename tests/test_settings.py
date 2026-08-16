"""Settings and the values derived from them: typed readers, the db
package split, error classification, notification wording and the
telemetry dimensions.

Merged from: test_settings_int.py, test_json_settings.py, test_db_package.py, test_error_explain.py, test_notification_wording.py, test_telemetry_source_dimension.py, test_telemetry_source_failures.py.
"""

import ast
import pathlib
import pytest

from mediaforge.web import error_explain as ee
from mediaforge.web import notifications as n
from mediaforge.telemetry import events, registry, settings
from mediaforge.telemetry.classify import is_transport_failure, is_user_cancellation


# ==========================================================================
# test_settings_int.py
#
# get_setting_int(): a bad value must not take a worker down.
# 
# An unparsable MEDIAFORGE_* value used to raise ValueError, which threw the
# auto-sync worker into its error branch on every cycle - sleep 30s, retry, never
# sync - and made the settings page answer 500.
# ==========================================================================
def test_reads_the_db_value(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("sync_error_retries", "3")
        assert db.get_setting_int("sync_error_retries", 0, "MEDIAFORGE_SYNC_ERROR_RETRIES") == 3


def test_falls_back_to_the_environment(app, monkeypatch):
    from mediaforge.web import db
    monkeypatch.setenv("MEDIAFORGE_TEST_INT", "7")
    with app.app_context():
        assert db.get_setting_int("does_not_exist", 0, "MEDIAFORGE_TEST_INT") == 7


def test_bad_environment_value_falls_back_to_the_default(app, monkeypatch):
    from mediaforge.web import db
    monkeypatch.setenv("MEDIAFORGE_TEST_INT", "drei")
    with app.app_context():
        assert db.get_setting_int("does_not_exist", 5, "MEDIAFORGE_TEST_INT") == 5


def test_bad_db_value_falls_back_to_the_default(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("broken_number", "abc")
        assert db.get_setting_int("broken_number", 42) == 42


def test_setting_int_empty_value_falls_back_to_the_default(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("empty_number", "")
        assert db.get_setting_int("empty_number", 9) == 9


# ==========================================================================
# test_json_settings.py
#
# get/set_json_setting(): a corrupt value must read as "unset", not raise.
# 
# List- and dict-valued settings used to be hand-encoded at every call site
# (json.dumps on save, json.loads on read, each with its own or no try/except),
# so one half-written or hand-edited row took down whatever read it.
# ==========================================================================
def test_round_trips_a_list(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_json_setting("json_list", ["a", "b"])
        assert db.get_json_setting("json_list", []) == ["a", "b"]


def test_round_trips_a_dict(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_json_setting("json_map", {"tok": 1})
        assert db.get_json_setting("json_map", {}) == {"tok": 1}


def test_missing_key_returns_the_default(app):
    from mediaforge.web import db
    with app.app_context():
        assert db.get_json_setting("json_never_written", []) == []
        assert db.get_json_setting("json_never_written") is None


def test_invalid_json_falls_back_to_the_default(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("json_broken", "{not json")
        assert db.get_json_setting("json_broken", []) == []


def test_empty_value_falls_back_to_the_default(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("json_empty", "")
        assert db.get_json_setting("json_empty", {}) == {}


def test_wrong_container_type_falls_back_to_the_default(app):
    """A caller asking for a list and getting a dict would break one line later."""
    from mediaforge.web import db
    with app.app_context():
        db.set_json_setting("json_shape", {"a": 1})
        assert db.get_json_setting("json_shape", []) == []


def test_default_is_copied_not_shared(app):
    """A caller mutating the result must not poison the next caller's default."""
    from mediaforge.web import db
    default = []
    with app.app_context():
        got = db.get_json_setting("json_never_written_2", default)
        got.append("x")
        assert db.get_json_setting("json_never_written_2", default) == []


def test_non_ascii_is_stored_readably(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_json_setting("json_umlaut", ["Grüße"])
        assert "Grüße" in db.get_setting("json_umlaut")
        assert db.get_json_setting("json_umlaut", []) == ["Grüße"]


def test_unserialisable_value_raises_at_the_write(app):
    from mediaforge.web import db
    with app.app_context():
        try:
            db.set_json_setting("json_bad_write", {1, 2})
        except TypeError:
            pass
        else:
            raise AssertionError("set_json_setting accepted a non-JSON value")
        assert db.get_setting("json_bad_write") is None


# ==========================================================================
# test_db_package.py
#
# Guard rails for the ``mediaforge.web.db`` package split.
# 
# The split of the former 6939-line ``db.py`` was a pure move, and these tests
# exist so it stays one. Two things can quietly undo it:
# 
# * a function moving between submodules, or a new one being added to a
#   submodule but not re-exported, which breaks ``from ..db import x`` at one
#   of ~40 call sites — a runtime ImportError nobody sees until that page is
#   opened;
# * an import cycle, which the current layout does not have and which would
#   otherwise be "fixed" by scattering lazy imports through the package.
# ==========================================================================
PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web" / "db"

# Names the package header binds for its own use. They were never public API
# even when everything lived in one file, and re-exporting them would make
# ``from ..db import json`` a supported thing to write.
_HEADER_NAMES = {
    "os", "re", "json", "sqlite3", "logger", "get_logger", "_dt",
    "check_password_hash", "generate_password_hash", "MEDIAFORGE_CONFIG_DIR",
    "_DEFAULT_MEDIA_KINDS", "annotations",
}


def _submodules():
    return sorted(p for p in PACKAGE.glob("*.py") if p.name != "__init__.py")


def _top_level_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names - _HEADER_NAMES


def test_package_is_not_empty():
    assert len(_submodules()) >= 15, "the db package lost its submodules"


def test_every_definition_is_re_exported():
    """``from ..db import x`` must keep working for everything the package defines."""
    from mediaforge.web import db

    missing = []
    for path in _submodules():
        for name in _top_level_names(path):
            if not hasattr(db, name):
                missing.append("%s.%s" % (path.stem, name))
    assert not missing, (
        "defined in a db submodule but not re-exported from db/__init__.py:\n  "
        + "\n  ".join(sorted(missing))
    )


def test_no_import_cycles():
    """A cycle would force lazy imports and make the load order load-bearing."""
    graph = {}
    for path in _submodules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        deps = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                deps.add(node.module)
        graph[path.stem] = deps

    state = {}
    cycles = []

    def visit(node, stack):
        state[node] = 1
        for dep in graph.get(node, ()):
            if state.get(dep) == 1:
                cycles.append(" -> ".join(stack + [node, dep]))
            elif dep in graph and state.get(dep) is None:
                visit(dep, stack + [node])
        state[node] = 2

    for module in graph:
        if state.get(module) is None:
            visit(module, [])

    assert not cycles, "import cycle(s) in the db package:\n  " + "\n  ".join(cycles)


def test_submodules_stay_reasonably_sized():
    """The point of the split. One file creeping back past ~1200 lines means a
    domain has grown enough to deserve its own module."""
    oversized = {
        path.name: sum(1 for _ in path.open(encoding="utf-8"))
        for path in _submodules()
        if sum(1 for _ in path.open(encoding="utf-8")) > 1200
    }
    assert not oversized, "db submodules that should be split further: %s" % oversized


@pytest.mark.parametrize("name", [
    # A spot-check across the domains, so a wholesale re-export failure is
    # reported as "get_setting is gone" rather than as 400 unrelated errors.
    "get_db", "DB_PATH", "init_db", "create_user", "verify_user", "USER_ROLES",
    "init_queue_db", "add_to_queue", "claim_next_queued", "cancel_queue_item",
    "get_setting", "set_setting", "get_json_setting", "set_json_setting",
    "is_sensitive_key", "register_sensitive_keys", "SENSITIVE_KEYS",
    "init_upscale_queue_db", "init_encoding_queue_db", "init_calendar_db",
    "get_uptime_heartbeats_between", "clear_user_ui_prefs",
])
def test_key_api_names_are_importable(name):
    from mediaforge.web import db
    assert hasattr(db, name), name


def test_old_single_file_module_is_gone():
    """A leftover db.py would be shadowed by the package and silently rot."""
    assert not (PACKAGE.parent / "db.py").exists()


# ==========================================================================
# test_error_explain.py
#
# Error classification and settings profiles.
# 
# The classifier is worth testing precisely because it is a pile of substring
# rules: the failure mode is not a crash, it is a confidently wrong explanation
# that sends somebody to fix the wrong thing.
# ==========================================================================
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


# ==========================================================================
# test_notification_wording.py
#
# Notification wording: DE/EN by UI language, and never "episodes" for a movie.
# 
# The bodies used to be hardcoded German, and a movie job / a single-episode job
# were both announced with series wording ("Episode(n)", "Neue Folgen").
# ==========================================================================
def test_tr_picks_german_only_for_de():
    assert n.tr("de", "Fehler", "Errors") == "Fehler"
    assert n.tr("en", "Fehler", "Errors") == "Errors"
    # Unknown/missing language falls back to English, like get_locale().
    assert n.tr(None, "Fehler", "Errors") == "Errors"
    assert n.tr("fr", "Fehler", "Errors") == "Errors"


def test_a_movie_is_never_called_an_episode():
    for lang in ("de", "en"):
        text = n.media_count_text(lang, 1, is_movie=True)
        assert "pisode" not in text and "Folge" not in text


def test_single_episode_is_singular():
    assert n.media_count_text("de", 1, False) == "1 Episode"
    assert n.media_count_text("en", 1, False) == "1 episode"
    assert n.media_count_text("de", 4, False) == "4 Episoden"
    assert n.media_count_text("en", 4, False) == "4 episodes"


def test_error_count_is_singular_for_one():
    assert n.error_count_text("en", 1) == "1 error"
    assert n.error_count_text("en", 3) == "3 errors"
    assert n.error_count_text("de", 1) == "1 Fehler"


def test_discord_movie_embed_has_no_episode_count(monkeypatch):
    """A movie is one file: the embed names it instead of counting episodes."""
    sent = {}

    monkeypatch.setattr(n, "_get_setting", lambda key, default="": {
        "notif_discord_webhook_url": "https://example.invalid/hook",
    }.get(key, default or "1"))
    monkeypatch.setattr(n, "_post_json", lambda url, payload, headers=None: sent.update(payload) or 204)
    # Send inline instead of on a daemon thread so the assertions can see it.
    monkeypatch.setattr(n.threading, "Thread",
                        lambda target=None, daemon=None: type("T", (), {"start": lambda s: target()})())

    n.notify_discord("Some Movie", "completed", episode_count=1, errors=[],
                     is_movie=True, lang="en")
    names = [f["name"] for f in sent["embeds"][0]["fields"]]
    values = [f["value"] for f in sent["embeds"][0]["fields"]]
    assert "Episodes" not in names and "Episoden" not in names
    assert "Movie" in values


if __name__ == "__main__":  # pragma: no cover - manual self-check
    test_tr_picks_german_only_for_de()
    test_a_movie_is_never_called_an_episode()
    test_single_episode_is_singular()
    test_error_count_is_singular_for_one()
    print("ok")


# ==========================================================================
# test_telemetry_source_dimension.py
#
# Which SITE a download came from -- the dimension the hoster never carried.
# 
# `build_download_event(provider=...)` is the HOSTER (VOE, MegaPlay). Several
# sites embed the same hosters, so it never answered whether anybody actually
# uses filmo.to, 9anime or aniwaves.ru.
# 
# The interesting half of these tests is what the builders REFUSE: the value
# ends up as a `feature_key` row and an indexed column on a public server, so
# anything outside the closed built-in list has to produce nothing at all.
# ==========================================================================
@pytest.fixture
def consented(monkeypatch):
    """Consent granted for the source dimension and the network detail key."""
    monkeypatch.setattr(settings, "is_key_enabled",
                        lambda key: key in ("flag.sources", "detail.network",
                                            "downloads.titles", "downloads.errors"))


@pytest.fixture
def no_consent(monkeypatch):
    monkeypatch.setattr(settings, "is_key_enabled", lambda key: False)


# ── the key mapping itself ──────────────────────────────────────────────────
@pytest.mark.parametrize("source_id, expected", [
    ("filmo", "flag.source.filmo"),
    ("nineanime", "flag.source.nineanime"),
    ("aniwaves", "flag.source.aniwaves"),
    ("aniworld", "flag.source.aniworld"),
    # The adult source keeps its own hard-limited key and must never gain a second.
    ("hanime", None),
    ("hanime_tv", None),
    # A module's source id is text its author chose; it must not become a
    # feature_key on a public server.
    ("my_module_source", None),
    ("../../etc/passwd", None),
    ("<script>", None),
    ("", None),
    (None, None),
])
def test_source_flag_key(source_id, expected):
    assert registry.source_flag_key(source_id) == expected


def test_consent_is_asked_once_for_the_whole_family():
    """One switch in the privacy dialog, one data_key per site on the wire --
    see the flag.sources registry entry for why the two differ."""
    assert registry.consent_key_for("flag.source.filmo") == "flag.sources"
    assert registry.consent_key_for("flag.source.aniwaves") == "flag.sources"
    # Everything else is governed by itself.
    assert registry.consent_key_for("flag.autosync") == "flag.autosync"
    assert registry.consent_key_for("downloads.titles") == "downloads.titles"


# ── the usage counter ───────────────────────────────────────────────────────
def test_a_built_in_source_produces_its_own_key(consented):
    event = events.build_source_usage_event("filmo")
    assert event and event["data_key"] == "flag.source.filmo"


@pytest.mark.parametrize("source_id", ["hanime", "hanime_tv", "my_module", "", None])
def test_nothing_is_built_for_a_source_we_may_not_name(consented, source_id):
    assert events.build_source_usage_event(source_id) is None


def test_without_consent_nothing_is_built(no_consent):
    assert events.build_source_usage_event("filmo") is None


# ── the download payload ────────────────────────────────────────────────────
def _download(**kwargs):
    return events.build_download_event(
        provider="VOE", media_type="series", title="Some Show", **kwargs)


def test_the_download_event_carries_the_site(consented):
    payloads = [e["payload"] for e in _download(source="filmo")]
    assert all(p["source"] == "filmo" for p in payloads)
    assert all(p["provider"] == "VOE" for p in payloads), "the hoster must survive too"


@pytest.mark.parametrize("source", ["hanime", "some_module", "", None, "x" * 200])
def test_an_unnameable_source_omits_the_field_rather_than_guessing(consented, source):
    for event in _download(source=source):
        assert "source" not in event["payload"]


def test_the_field_is_simply_absent_for_an_old_call_site(consented):
    """Every existing caller omits `source` entirely -- that must keep working
    and must not start sending a placeholder."""
    for event in _download():
        assert "source" not in event["payload"]


# ── network problems ────────────────────────────────────────────────────────
def test_dns_fallback_carries_no_host(consented):
    event = events.build_network_detail_event("dns_fallback")
    assert event and event["payload"]["action"] == "dns_fallback"
    # The hostname would say which site was being visited; the resolver name is
    # the user's own network configuration. Neither belongs in this event.
    assert "metadata" not in event["payload"]


def test_a_source_outage_names_the_source(consented):
    event = events.build_network_detail_event("source_unavailable", "aniwaves")
    assert event["payload"]["metadata"] == {"source": "aniwaves"}


def test_an_outage_of_something_we_cannot_name_is_still_counted(consented):
    """Anonymously: the count is useful, a module's id is not ours to send."""
    event = events.build_network_detail_event("source_unavailable", "some_module")
    assert event["payload"]["metadata"] == {"source": "other"}


def test_an_unknown_action_is_not_invented(consented):
    """`action` lands in an indexed column, so a typo at a call site must not
    be able to create a new server-side value."""
    assert events.build_network_detail_event("oops") is None


def test_network_events_need_their_own_consent(no_consent):
    assert events.build_network_detail_event("dns_fallback") is None


# ==========================================================================
# test_telemetry_source_failures.py
#
# A source site having a bad day is not a MediaForge crash.
# 
# Every site model raises its own ``<Site>Unavailable`` when the page it got back
# was not usable -- a 404, a challenge interstitial, a maintenance page. Those
# reach an ERROR log (routes/search.py logs the provider/season fetch failures
# with exc_info), and hooks._TelemetryLogHandler turns ERROR records into
# stage-1 crash reports. Without classification, every flaky moment on
# filmo.to / 9anime / aniwaves.ru would be filed as a defect in this app --
# the same noise the yt-dlp error handler was already changed to avoid.
# ==========================================================================
@pytest.mark.parametrize("module_path, exc_name, message", [
    ("mediaforge.models.filmo_to.scraper", "FilmoUnavailable", "Movie not found (HTTP 404): x"),
    ("mediaforge.models.filmo_to.scraper", "FilmoTokenExpired", "rejected the CSRF token"),
    ("mediaforge.models.nineanime_to.scraper", "NineAnimeUnavailable", "Not found (HTTP 404): /x"),
    ("mediaforge.models.aniwaves_ru.scraper", "AniwavesUnavailable", "Series not found"),
    ("mediaforge.models.megakino_to.scraper", "MegakinoUnavailable", "challenge page"),
])
def test_site_unavailable_is_kept_out_of_the_crash_channel(module_path, exc_name, message):
    import importlib

    mod = importlib.import_module(module_path)
    exc_type = getattr(mod, exc_name)
    assert is_transport_failure(exc_type, exc_type(message)) is True


def test_a_real_defect_is_still_reported():
    """The filter must stay narrow: a scraper whose regex stopped matching is
    exactly the kind of breakage the crash channel exists for."""
    assert is_transport_failure(ValueError, ValueError("No video source found")) is False
    assert is_transport_failure(KeyError, KeyError("provider_data")) is False
    assert is_transport_failure(AttributeError, AttributeError("'NoneType' has no 'group'")) is False


def test_user_cancellation_is_still_not_a_crash():
    """Unchanged, but pinned next to the above: the project's rule is that a
    user aborting their own download never produces a report."""
    assert is_user_cancellation(message="Download cancelled") is True
