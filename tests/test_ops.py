"""Operations: migrations, snapshots, the audit log, groups and rules, the
external worker host, the UpTime monitor, Auto-Sync and watch parties.

Merged from: test_ops.py, test_worker_host.py, test_uptime_monitor.py, test_autosync_dryrun.py, test_syncplay_social.py.
"""

import json
import pytest
import threading
import time

from mediaforge.web import worker_host, worker_registry
from mediaforge.web import syncplay_rooms as sp


# ==========================================================================
# test_ops.py
#
# Operations layer: migrations, snapshots, audit log, groups, rules,
# language profiles and maintenance windows.
# 
# These cover the behaviour that is easy to break silently and expensive to
# notice late -- an audit row that can be deleted, a group whose scope is
# ignored, a rule that matches when it should not -- rather than the CRUD
# plumbing, which the route smoke test already exercises.
# ==========================================================================
# ---------------------------------------------------------------------------
# Migrations and snapshots
# ---------------------------------------------------------------------------

def test_schema_is_at_latest_version(app):
    from mediaforge.web import dbmigrate
    with app.app_context():
        status = dbmigrate.status()
    assert status["current"] == dbmigrate.latest_version()
    assert status["pending"] == []


def test_migration_numbers_are_unique_and_contiguous():
    """A gap is legal but almost always a mistake (a migration was deleted).

    Reusing a number is never legal: databases that already recorded it would
    skip the new migration entirely.
    """
    from mediaforge.web import dbmigrate
    versions = dbmigrate.known_versions()
    assert versions == sorted(set(versions))
    assert versions[0] == 1
    assert versions == list(range(1, len(versions) + 1))


def test_run_pending_is_idempotent(app):
    from mediaforge.web import dbmigrate
    with app.app_context():
        result = dbmigrate.run_pending()
    assert result["ok"]
    assert result["applied"] == []


def test_a_migration_recorded_but_not_applied_is_repaired(app, tmp_path):
    """Regression, from a real incident.

    An early version of run_pending() baselined *everything* pending rather
    than stopping at BASELINE_VERSION. Databases that started the app once
    with that build came away with migrations 2..7 marked applied and none of
    their tables created -- and the fixed engine then correctly believed it
    had nothing to do, so the app failed at runtime with "no such table:
    worker_heartbeats". The record is therefore not trusted on its own.
    """
    import sqlite3

    from mediaforge.web import dbmigrate

    broken = tmp_path / "broken.db"
    conn = sqlite3.connect(str(broken))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL, app_version TEXT NOT NULL DEFAULT '',"
        " baselined INTEGER NOT NULL DEFAULT 0)")
    for version in dbmigrate.known_versions():
        conn.execute("INSERT INTO schema_migrations VALUES (?,?,?,?,1)",
                     (version, "x", "2026-01-01", "1.4.3"))
    conn.commit()

    reset = dbmigrate.repair_missing(conn)
    assert reset, "the repair pass did not notice the missing tables"
    # ...and only the ones that own tables are reset. Migration 1 is a no-op
    # baseline and must not be re-run for the sake of it.
    assert 1 not in reset

    still_recorded = {r["version"] for r in
                      conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for version in reset:
        assert version not in still_recorded
    conn.close()


def test_repair_leaves_a_healthy_database_alone(app):
    """It runs on every start, so it has to be a no-op when nothing is wrong."""
    from mediaforge.web import dbmigrate
    from mediaforge.web.db import get_db

    with app.app_context():
        conn = get_db()
        try:
            assert dbmigrate.repair_missing(conn) == []
        finally:
            conn.close()


def test_snapshot_verify_and_traversal_guard(app):
    from mediaforge.web import dbmigrate
    with app.app_context():
        meta = dbmigrate.snapshot(reason="manual", note="pytest")
        assert meta and meta["size"] > 0

        check = dbmigrate.verify_snapshot(meta["id"])
        assert check["ok"], check
        assert check["integrity"] == "ok"
        assert check["schema_version"] == dbmigrate.latest_version()

        # The id arrives from an HTTP route, so it is untrusted.
        for evil in ("../../mediaforge", "..\\..\\mediaforge", "/etc/passwd", ""):
            assert dbmigrate._snapshot_path(evil) is None

        assert dbmigrate.delete_snapshot(meta["id"])
        assert dbmigrate.verify_snapshot(meta["id"])["ok"] is False


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_audit_is_append_only(app):
    """The trigger, not just application code, has to refuse edits."""
    import sqlite3

    from mediaforge.web import audit

    with app.app_context():
        audit.init_audit_db()
        audit.audit("system", "pytest_marker", target="append-only")
        audit.flush()

        conn = sqlite3.connect(str(audit.AUDIT_DB_PATH))
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM audit_log")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE audit_log SET action = 'tampered'")
        finally:
            conn.close()


def test_audit_redacts_secret_shaped_keys(app):
    from mediaforge.web import audit
    with app.app_context():
        audit.init_audit_db()
        audit.audit("settings", "setting_changed",
                    detail={"seerr_api_key": "super-secret",
                            "notif_telegram_bot_token": "123:ABC",
                            "nested": {"password": "hunter2"},
                            "enabled": True})
        audit.flush()
        entry = audit.query(action="setting_changed", limit=1)["entries"][0]

    assert entry["detail"]["seerr_api_key"] == "<redacted>"
    assert entry["detail"]["notif_telegram_bot_token"] == "<redacted>"
    assert entry["detail"]["nested"]["password"] == "<redacted>"
    # The non-secret value must survive, or the log stops being useful.
    assert entry["detail"]["enabled"] is True


def test_audit_hash_chain_detects_tampering(app):
    import sqlite3

    from mediaforge.web import audit

    with app.app_context():
        audit.init_audit_db()
        for i in range(5):
            audit.audit("system", "chain_probe", target=str(i))
        audit.flush()
        assert audit.verify_chain()["ok"]

        # Rewrite a row behind the trigger's back, the way anybody with file
        # access could. The chain is what makes that visible.
        conn = sqlite3.connect(str(audit.AUDIT_DB_PATH))
        try:
            conn.execute("DROP TRIGGER audit_no_update")
            row = conn.execute(
                "SELECT id FROM audit_log WHERE action='chain_probe' "
                "ORDER BY id LIMIT 1 OFFSET 2").fetchone()
            conn.execute("UPDATE audit_log SET target='tampered' WHERE id = ?", (row[0],))
            conn.commit()
        finally:
            conn.close()

        broken = audit.verify_chain()
        assert broken["ok"] is False
        assert broken["broken_at"] == row[0]


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

def test_builtin_groups_are_readonly(app):
    from mediaforge.web import groups
    with app.app_context():
        builtins = [g for g in groups.list_groups() if g["builtin"]]
        assert {g["key"] for g in builtins} == {"admin", "user", "kids"}
        for group in builtins:
            ok, err = groups.update_group(group["id"], permissions=["queue.write"])
            assert not ok and err == "builtin_readonly"
            ok, err = groups.delete_group(group["id"])
            assert not ok and err == "builtin_readonly"


def test_unknown_permissions_are_dropped(app):
    """A permission nothing checks must never be storable.

    Otherwise the group editor shows it as granted and it does nothing, which
    reads as "this account is restricted" when it is not.
    """
    from mediaforge.web import groups
    with app.app_context():
        gid, err = groups.create_group(
            "pytest_perm", "Pytest perms",
            ["queue.write", "not.a.real.permission", "*"], ["*"])
        assert err is None
        stored = groups.get_group(gid)["permissions"]
        assert stored == ["queue.write"]
        groups.delete_group(gid)


def test_kids_group_has_no_adult_permission(app):
    from mediaforge.web import groups
    with app.app_context():
        assert groups.has_permission(999, "kids", "adult.view") is False
        assert groups.has_permission(999, "user", "adult.view") is True


def test_explicit_scope_beats_wildcard(app, users):
    """The whole point of scoping.

    Every user is in a built-in group whose scope is ["*"], so a naive
    "wildcard wins" rule would make a scoped group do nothing at all.
    """
    from mediaforge.web import groups
    with app.app_context():
        gid, err = groups.create_group("pytest_scope", "Scoped", ["library.read"], ["lib-a"])
        assert err is None
        try:
            uid = users["user"]
            assert groups.effective_scope(uid, "user") == ["*"]
            groups.set_user_groups(uid, [gid])
            assert groups.effective_scope(uid, "user") == ["lib-a"]
            assert groups.scope_allows(["lib-a"], "lib-a") is True
            assert groups.scope_allows(["lib-a"], "lib-b") is False
            # An admin is never scoped: one who cannot see a library cannot
            # fix it either.
            assert groups.effective_scope(uid, "admin") == ["*"]
        finally:
            groups.set_user_groups(users["user"], [])
            groups.delete_group(gid)


def test_deleting_a_user_purges_group_memberships(app):
    """A recycled user id must not inherit the previous account's rights."""
    from mediaforge.web import db, groups
    with app.app_context():
        gid, _ = groups.create_group("pytest_purge", "Purge", ["queue.write"], ["*"])
        uid = db.create_user("pytest-purge-user", "pytest-pw-12345", role="user")
        groups.set_user_groups(uid, [gid])
        assert groups.user_group_ids(uid) == [gid]

        ok, err = db.delete_user(uid)
        assert ok, err
        assert groups.user_group_ids(uid) == []
        groups.delete_group(gid)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def test_rule_conditions_are_and_ed():
    from mediaforge.web.rules import evaluate
    rules = [{
        "name": "both", "enabled": True, "priority": 10, "stop": False,
        "conditions": [
            {"field": "provider", "op": "is", "value": "aniworld"},
            {"field": "language", "op": "is", "value": "de"},
        ],
        "actions": {"quality": "1080p"},
    }]
    assert evaluate({"provider": "aniworld", "language": "de"}, rules)["quality"] == "1080p"
    assert "quality" not in evaluate({"provider": "aniworld", "language": "en"}, rules)


def test_unknown_condition_field_fails_closed():
    """A rule the engine cannot evaluate must not become "always true"."""
    from mediaforge.web.rules import evaluate
    rules = [{"name": "bogus", "enabled": True, "priority": 10, "stop": False,
              "conditions": [{"field": "not_a_field", "op": "is", "value": "x"}],
              "actions": {"skip": True}}]
    assert "skip" not in evaluate({"title": "anything"}, rules)


def test_rule_priority_and_stop():
    from mediaforge.web.rules import evaluate
    rules = [
        {"name": "first", "enabled": True, "priority": 1, "stop": False,
         "conditions": [], "actions": {"quality": "720p", "notify": True}},
        {"name": "second", "enabled": True, "priority": 2, "stop": True,
         "conditions": [], "actions": {"quality": "1080p"}},
        {"name": "never", "enabled": True, "priority": 3, "stop": False,
         "conditions": [], "actions": {"quality": "480p"}},
    ]
    result = evaluate({}, rules)
    assert result["quality"] == "1080p"      # later match overrides
    assert result["notify"] is True          # earlier action survives
    assert result["_matched"] == ["first", "second"]   # "never" not reached


def test_invalid_regex_does_not_raise():
    from mediaforge.web.rules import evaluate
    rules = [{"name": "bad-re", "enabled": True, "priority": 1, "stop": False,
              "conditions": [{"field": "title", "op": "matches", "value": "([unclosed"}],
              "actions": {"skip": True}}]
    assert evaluate({"title": "anything"}, rules)["_matched"] == []


def test_unknown_actions_are_dropped_on_save(app):
    from mediaforge.web.rules import delete_rule, get_rule, save_rule
    with app.app_context():
        rid, err = save_rule({
            "name": "pytest-rule",
            "conditions": [{"field": "title", "op": "contains", "value": "x"}],
            "actions": {"quality": "1080p", "rm_rf": "/", "skip": True},
        })
        assert err is None
        stored = get_rule(rid)["actions"]
        assert "rm_rf" not in stored
        assert stored["quality"] == "1080p" and stored["skip"] is True
        delete_rule(rid)


# ---------------------------------------------------------------------------
# Language profiles
# ---------------------------------------------------------------------------

def test_language_chain_keeps_order_and_dedupes(app):
    from mediaforge.web.langprofiles import delete_profile, get_profile, save_profile
    with app.app_context():
        pid, err = save_profile({"name": "pytest-chain", "chain": ["DE", "en", "de", " ja "]})
        assert err is None
        assert get_profile(pid)["chain"] == ["de", "en", "ja"]
        delete_profile(pid)


def test_profile_resolution_order(app):
    from mediaforge.web.langprofiles import (bind_title, delete_profile,
                                             resolve, save_profile)
    with app.app_context():
        title_pid, _ = save_profile({"name": "pytest-title", "chain": ["de"]})
        rule_pid, _ = save_profile({"name": "pytest-rule-profile", "chain": ["ja"]})
        url = "https://example.invalid/anime/pytest"
        try:
            assert resolve(url, fallback="en")["source"] == "global"
            bind_title(url, title_pid, "Pytest")
            assert resolve(url, fallback="en")["source"] == "title"
            assert resolve(url, fallback="en")["chain"] == ["de"]
            # A rule wins over the per-title binding: that is what makes bulk
            # rules useful without taking away per-title control.
            hit = resolve(url, rule_profile_id=rule_pid, fallback="en")
            assert hit["source"] == "rule" and hit["chain"] == ["ja"]
        finally:
            bind_title(url, None)
            delete_profile(title_pid)
            delete_profile(rule_pid)


# ---------------------------------------------------------------------------
# Maintenance windows
# ---------------------------------------------------------------------------

def test_window_wraps_over_midnight():
    import datetime as dt

    from mediaforge.web.maintenance import _covers

    night = {"enabled": 1, "days_mask": 127, "start_minute": 22 * 60,
             "end_minute": 6 * 60}
    assert _covers(night, dt.datetime(2026, 8, 5, 23, 0).weekday(), 23 * 60)
    assert _covers(night, 0, 2 * 60)
    assert not _covers(night, 0, 12 * 60)


def test_overlapping_windows_take_the_strictest(app, monkeypatch):
    from mediaforge.web import maintenance
    rows = [
        {"name": "a", "enabled": 1, "days_mask": 127, "start_minute": 0,
         "end_minute": 1440, "max_downloads": 3, "allow_encoding": 1,
         "allow_upscale": 1, "allow_scan": 1},
        {"name": "b", "enabled": 1, "days_mask": 127, "start_minute": 0,
         "end_minute": 1440, "max_downloads": 1, "allow_encoding": 0,
         "allow_upscale": 1, "allow_scan": 1},
    ]
    monkeypatch.setattr(maintenance, "_rows", lambda: rows)
    limits = maintenance.current_limits()
    assert limits["max_downloads"] == 1
    assert limits["allow_encoding"] is False
    assert limits["allow_upscale"] is True
    assert maintenance.max_downloads(8) == 1
    assert maintenance.is_allowed("encoding") is False


def test_no_window_means_no_override(app, monkeypatch):
    """None must mean "use the configured setting", never "unlimited"."""
    from mediaforge.web import maintenance
    monkeypatch.setattr(maintenance, "_rows", lambda: [])
    assert maintenance.current_limits()["max_downloads"] is None
    assert maintenance.max_downloads(4) == 4
    assert maintenance.is_allowed("encoding") is True


# ---------------------------------------------------------------------------
# Authorisation of the new endpoints
# ---------------------------------------------------------------------------

def test_ops_endpoints_are_all_admin_gated(app):
    """Every endpoint routes/ops.py registers is in the app's admin set.

    The set is hand-maintained (see app.py), so this is the assertion that
    catches a new endpoint being added without being protected.
    """
    from mediaforge.web.routes.ops import ADMIN_ONLY_OPS_ENDPOINTS
    admin_only = app.config["ADMIN_ONLY_ENDPOINTS"]
    missing = sorted(ADMIN_ONLY_OPS_ENDPOINTS - set(admin_only))
    assert not missing, "ops endpoints not admin-gated: %s" % missing

    registered = {r.endpoint for r in app.url_map.iter_rules()}
    unregistered = sorted(ADMIN_ONLY_OPS_ENDPOINTS - registered)
    assert not unregistered, "listed but never registered: %s" % unregistered


def test_normal_user_cannot_read_the_audit_log(as_user):
    client = as_user("user")
    assert client.get("/api/ops/audit").status_code == 403
    assert client.get("/api/ops/groups").status_code == 403
    assert client.get("/api/ops/diagnostics").status_code == 403


def test_health_endpoints_answer_without_a_session(client):
    for path in ("/healthz", "/readyz"):
        resp = client.get(path)
        assert resp.status_code in (200, 503)
        body = json.loads(resp.data)
        # Booleans and a status string only -- no version, no worker names.
        assert set(body) == {"status"}


# ---------------------------------------------------------------------------
# Worker cards
# ---------------------------------------------------------------------------

def test_worker_links_are_relative_same_origin_paths():
    """The card renders this into an href. A module registering a worker must
    never be able to turn an Operations card into an outbound link."""
    import re

    from mediaforge.web import worker_registry as wr
    for name, meta in wr.WORKERS.items():
        link = meta.get("link")
        if link is None:
            continue
        assert re.fullmatch(r"/[A-Za-z0-9/_-]*", link), (name, link)


def test_snapshot_never_lets_a_heartbeat_row_supply_a_link(app):
    """snapshot() does entry.update(row); a stray column must not win."""
    from mediaforge.web import worker_registry as wr
    with app.app_context():
        wr.beat("autosync", detail="pytest")
        entry = [w for w in wr.snapshot() if w["worker"] == "autosync"][0]
        assert entry["link"] == "/autosync"
        unlisted = [w for w in wr.snapshot() if w["worker"] not in wr.WORKERS]
        assert all(w["link"] == "" for w in unlisted)


def test_uptime_card_says_when_it_looks_again():
    """"5 up / 0 down" next to a bare "Inactive" is what read as a fault."""
    from mediaforge.web import worker_registry as wr
    assert wr.F_NEXT_RUN in wr.WORKERS["uptime"]["fields"]


def test_autosync_has_no_next_run_field():
    """There is no "the" next auto-sync run.

    Every job carries its own interval, weekly slot and retry backoff, so the
    earliest of forty-two of them is a number that answers a question nobody
    asked. The card links to /autosync, which shows the real schedule per job.
    """
    from mediaforge.web import worker_registry as wr
    assert wr.F_NEXT_RUN not in wr.WORKERS["autosync"]["fields"]
    assert wr.WORKERS["autosync"]["link"] == "/autosync"


def test_admin_can_read_the_ops_apis(as_user):
    client = as_user("admin")
    for path in ("/api/ops/audit", "/api/ops/groups", "/api/ops/schema",
                 "/api/ops/workers", "/api/ops/maintenance", "/api/ops/rules",
                 "/api/ops/language-profiles", "/api/ops/audit/stats"):
        assert client.get(path).status_code == 200, path


# ==========================================================================
# test_worker_host.py
#
# Worker heartbeats and the external worker host.
# 
# The heartbeat test is a regression test with a story: the first version of
# ``beat()`` wrote NULL into ``worker_heartbeats.last_error``, which is NOT NULL.
# Every single heartbeat failed, ``beat()`` swallowed the error by design (a
# heartbeat must never take a worker down), and the symptom was an Operations
# view that was simply empty. Nothing in the log at anything above DEBUG.
# ==========================================================================
# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------

def _row(app, worker):
    from mediaforge.web.db import get_db
    with app.app_context():
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM worker_heartbeats WHERE worker = ?", (worker,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def test_first_heartbeat_actually_writes_a_row(app):
    """The INSERT branch. This is the one that used to fail on every call."""
    with app.app_context():
        worker_registry.beat("pytest-fresh", state="idle", detail="hello")
    row = _row(app, "pytest-fresh")
    assert row is not None, "beat() silently wrote nothing"
    assert row["state"] == "idle"
    assert row["detail"] == "hello"
    assert row["last_error"] == ""      # NOT NULL: never NULL, even with no error


def test_heartbeat_updates_in_place(app):
    with app.app_context():
        worker_registry.beat("pytest-update", state="idle")
        worker_registry.working("pytest-update", detail="job 7")
    row = _row(app, "pytest-update")
    # "working", not "running": the Operations view knows exactly three states
    # (idle / working / error) and beat() normalizes onto them on write, so a
    # legacy caller passing "running" also lands here.
    assert row["state"] == worker_registry.STATE_WORKING
    assert row["detail"] == "job 7"


def test_legacy_state_names_are_normalized_on_write(app):
    """A module's worker (or an old call site) may still say "running"."""
    with app.app_context():
        worker_registry.beat("pytest-legacy", state="running")
        assert _row(app, "pytest-legacy")["state"] == worker_registry.STATE_WORKING
        worker_registry.beat("pytest-legacy", state="unknown")
        assert _row(app, "pytest-legacy")["state"] == worker_registry.STATE_IDLE


def test_extras_merge_instead_of_replacing(app):
    """A "still working" beat must not wipe the count the last run reported."""
    with app.app_context():
        worker_registry.done("pytest-extra", extra={"found": 42})
        worker_registry.working("pytest-extra", detail="scanning")
        entry = [w for w in worker_registry.snapshot()
                 if w["worker"] == "pytest-extra"][0]
    assert entry["extra"]["found"] == 42


def test_a_working_worker_gets_a_stall_deadline_and_an_idle_one_does_not(app):
    with app.app_context():
        worker_registry.working("upscale", detail="episode 1")
        working = [w for w in worker_registry.snapshot()
                   if w["worker"] == "upscale"][0]
        assert working["stall_deadline"], "a working worker must be watched"
        assert not worker_registry.is_stalled(working)

        # Same worker, idle: nothing to count down to. This is the false
        # "overdue" the old fixed-window check produced.
        worker_registry.done("upscale")
        idle = [w for w in worker_registry.snapshot()
                if w["worker"] == "upscale"][0]
        assert idle["stall_deadline"] is None
        assert not worker_registry.is_stalled(idle)


def test_scheduled_workers_are_never_watched_for_stalls(app):
    """Sleeping for an hour between rounds is waiting, not stalling."""
    assert worker_registry.stall_after("autosync") is None
    assert worker_registry.stall_after("library_scan") is None
    assert worker_registry.stall_after("queue")


def test_errors_are_sticky_until_explicitly_cleared(app):
    """A failure at 4 a.m. has to still be visible in the morning."""
    with app.app_context():
        worker_registry.fail("pytest-sticky", "boom", detail="job 3")
        assert _row(app, "pytest-sticky")["last_error"] == "boom"
        assert _row(app, "pytest-sticky")["error_at"]

        # A plain heartbeat must not wipe it.
        worker_registry.beat("pytest-sticky", state="idle")
        assert _row(app, "pytest-sticky")["last_error"] == "boom"

        # done() clears it explicitly.
        worker_registry.done("pytest-sticky", detail="recovered")
        assert _row(app, "pytest-sticky")["last_error"] == ""


def test_last_run_is_not_erased_by_a_plain_heartbeat(app):
    with app.app_context():
        worker_registry.done("pytest-lastrun")
        first = _row(app, "pytest-lastrun")["last_run"]
        assert first
        worker_registry.beat("pytest-lastrun", state="idle")
        assert _row(app, "pytest-lastrun")["last_run"] == first


def test_snapshot_lists_known_workers_even_when_silent(app):
    """"never started" and "does not exist" are different problems."""
    with app.app_context():
        names = {w["worker"] for w in worker_registry.snapshot()}
    assert set(worker_registry.WORKERS) <= names
    with app.app_context():
        silent = [w for w in worker_registry.snapshot()
                  if w["worker"] == "mediascan"]
    # A worker that has never reported is idle, not "unknown" -- see the
    # STATE_* constants in worker_registry for why that state is gone.
    assert silent and silent[0]["state"] in (
        worker_registry.STATE_IDLE,
        worker_registry.STATE_WORKING,
        worker_registry.STATE_ERROR,
    )


def test_beat_never_raises(app, monkeypatch):
    """Diagnostics must not be able to take a worker down."""
    def _boom():
        raise RuntimeError("database is gone")
    monkeypatch.setattr("mediaforge.web.db.get_db", _boom)
    with app.app_context():
        worker_registry.beat("pytest-safe", state="idle")   # must not raise


# ---------------------------------------------------------------------------
# Worker host
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, "inprocess"),
    ("", "inprocess"),
    ("inprocess", "inprocess"),
    ("anything-else", "inprocess"),
    ("external", "external"),
    ("EXTERNAL", "external"),
    ("separate", "external"),
    ("host", "external"),
])
def test_worker_mode_defaults_to_in_process(monkeypatch, value, expected):
    """Doing nothing must keep the old behaviour. This is the safety property."""
    monkeypatch.delenv("MEDIAFORGE_WORKER_MODE", raising=False)
    if value is not None:
        monkeypatch.setenv("MEDIAFORGE_WORKER_MODE", value)
    assert worker_host.worker_mode() == expected
    assert worker_host.workers_run_in_web_process() is (expected == "inprocess")


def test_selected_workers_defaults_to_all(monkeypatch):
    monkeypatch.delenv("MEDIAFORGE_WORKERS", raising=False)
    assert worker_host.selected_workers() == list(worker_host.DEFAULT_WORKERS)


def test_selected_workers_honours_the_list(monkeypatch):
    monkeypatch.setenv("MEDIAFORGE_WORKERS", "queue, encoding")
    assert worker_host.selected_workers() == ["queue", "encoding"]


def test_unknown_worker_names_fall_back_to_all(monkeypatch, caplog):
    """A typo must not silently mean "run nothing" -- the symptom would be
    downloads sitting there with nothing in the log."""
    monkeypatch.setenv("MEDIAFORGE_WORKERS", "queeu")
    assert worker_host.selected_workers() == list(worker_host.DEFAULT_WORKERS)


def test_every_declared_worker_resolves():
    """The dotted paths are strings, so nothing checks them until runtime."""
    for name, path in worker_host._WORKERS.items():
        fn = worker_host._resolve(path)
        assert callable(fn), name


def test_host_workers_are_a_subset_of_the_registry():
    """A worker the host can run but the Operations view does not know about
    would be invisible exactly when it is the one that is stuck."""
    unknown = set(worker_host._WORKERS) - set(worker_registry.WORKERS)
    assert not unknown, unknown


# ---------------------------------------------------------------------------
# Stall watchdog
# ---------------------------------------------------------------------------

def test_a_dead_worker_thread_is_noticed_even_though_it_reported_idle(app, monkeypatch):
    """The failure the first version of the watchdog could not see.

    A worker that unwinds on request (or crashes) last reported "idle", and an
    idle worker has no stall deadline -- so a watchdog that only looked at
    stall deadlines would never restart the one failure it can fully repair.
    """
    from mediaforge.web import worker_watchdog as wd

    restarted = []
    monkeypatch.setattr(wd, "_thread_alive", lambda name: False)
    monkeypatch.setattr(wd, "_restart",
                        lambda worker, entry, age: restarted.append(worker) or "restarted")
    monkeypatch.setattr(wd, "_audit", lambda *a, **k: None)

    with app.app_context():
        worker_registry.idle("queue")
        acted = wd.check_once()

    assert "queue" in acted
    assert "queue" in restarted


def test_a_live_idle_worker_is_left_alone(app, monkeypatch):
    from mediaforge.web import worker_watchdog as wd

    touched = []
    monkeypatch.setattr(wd, "_thread_alive", lambda name: True)
    monkeypatch.setattr(wd, "_restart",
                        lambda worker, entry, age: touched.append(worker) or "restarted")
    monkeypatch.setattr(wd, "_audit", lambda *a, **k: None)

    with app.app_context():
        worker_registry.idle("queue")
        worker_registry.idle("encoding")
        worker_registry.idle("upscale")
        assert wd.check_once() == []
    assert touched == []


def test_worker_exiting_clears_the_started_flag(monkeypatch):
    """Without this the watchdog's own restart request kills a worker for good:
    the thread ends, the module still thinks it started one, and
    _ensure_*_worker() refuses to start another for the life of the process."""
    from mediaforge.web import queue_worker
    from mediaforge.web import worker_watchdog as wd

    queue_worker._queue_worker_started = True
    wd._event_for("queue").set()

    wd.worker_exiting("queue")

    assert queue_worker._queue_worker_started is False
    assert not wd.restart_requested("queue")


def test_publishing_is_deduplicated(app, monkeypatch):
    """An idle worker beats every few seconds. Publishing each one would make
    every connected Operations view rebuild a full snapshot for no change."""
    published = []
    monkeypatch.setattr(worker_registry, "_last_published", {})
    from mediaforge.web import events

    monkeypatch.setattr(events, "publish", lambda topic, payload: published.append(payload))

    with app.app_context():
        worker_registry.idle("pytest-dedup", detail="waiting")
        worker_registry.idle("pytest-dedup", detail="waiting")
        worker_registry.idle("pytest-dedup", detail="waiting")
        assert len(published) == 1, "an unchanged heartbeat must not publish"

        worker_registry.working("pytest-dedup", detail="job 1")
        assert len(published) == 2, "a real change must publish"


# ==========================================================================
# test_uptime_monitor.py
#
# UpTime monitor: probe verdicts, bucketing, defaults and the round lock.
# 
# Each test here pins one of the bugs the July 2026 audit found, so a
# regression shows up as a named failure rather than as a wrong number on a
# dashboard nobody stares at.
# ==========================================================================
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


# ==========================================================================
# test_autosync_dryrun.py
#
# Auto-Sync dry run.
# 
# The property that matters is not "it returns a plan" — it is that running the
# preview does not change the thing it was asked to describe. A dry run that
# updates ``last_check`` and clears the "new episodes" badge means the next real
# run finds nothing new, which is a data-loss-shaped bug wearing a preview's
# clothes.
# ==========================================================================
@pytest.fixture()
def job(app):
    from mediaforge.web.db import (add_autosync_job, remove_autosync_job,
                                   get_autosync_job, update_autosync_job)
    with app.app_context():
        job_id = add_autosync_job(
            title="Pytest Dry Run",
            series_url="https://example.invalid/anime/pytest-dry-run",
            language="German Dub",
            provider="VOE",
            added_by="test-admin",
        )
        # Give it a baseline the dry run must not touch.
        update_autosync_job(job_id, last_check="2026-01-01 00:00:00",
                            episodes_found=12, last_new_count=3)
        yield get_autosync_job(job_id)
        remove_autosync_job(job_id)


def _run(app, job, **kwargs):
    from mediaforge.web.autosync_worker import _run_autosync_for_job
    with app.app_context():
        return _run_autosync_for_job(job, **kwargs)


def test_dry_run_returns_a_report_even_when_it_cannot_reach_the_provider(app, job):
    """example.invalid never resolves, which is the realistic offline case.

    The report still has to come back and say it failed, rather than raising
    or returning None -- the UI has nothing else to show.
    """
    report = _run(app, job, dry_run=True)
    assert isinstance(report, dict)
    assert report["job_id"] == job["id"]
    assert report["dry_run"] is True
    assert "would_queue" in report


def test_dry_run_does_not_touch_the_job(app, job):
    """The whole point. Checked field by field, because "mostly unchanged" is
    what this bug looks like when it comes back."""
    from mediaforge.web.db import get_autosync_job

    before = dict(get_autosync_job(job["id"]))
    _run(app, job, dry_run=True)
    after = dict(get_autosync_job(job["id"]))

    for field in ("last_check", "episodes_found", "local_episodes_found",
                  "last_new_found", "last_new_count", "retry_count",
                  "filter_dirty"):
        assert before.get(field) == after.get(field), field


def test_dry_run_queues_nothing(app, job):
    from mediaforge.web.db import get_queue

    with app.app_context():
        before = len(get_queue())
    _run(app, job, dry_run=True)
    with app.app_context():
        assert len(get_queue()) == before


def test_dry_run_implies_no_queueing_even_if_asked(app, job):
    """dry_run must win over queue_downloads, not the other way round."""
    from mediaforge.web.db import get_queue
    with app.app_context():
        before = len(get_queue())
    _run(app, job, dry_run=True, queue_downloads=True)
    with app.app_context():
        assert len(get_queue()) == before


def test_report_is_filled_in_place(app, job):
    """The caller passes the dict in so an early exit still reports something."""
    from mediaforge.web.autosync_worker import _run_autosync_for_job
    report = {}
    with app.app_context():
        returned = _run_autosync_for_job(job, dry_run=True, report=report)
    assert returned is report
    assert report["title"] == "Pytest Dry Run"


def test_blocked_run_says_why(app, job, monkeypatch):
    monkeypatch.setattr("mediaforge.web.autosync_worker.is_layout_backoff_active",
                        lambda: True)
    monkeypatch.setattr("mediaforge.web.autosync_worker.layout_backoff_remaining",
                        lambda: 300.0)
    report = _run(app, job, dry_run=True)
    assert report["blocked"] == "layout_backoff"


def test_endpoint_requires_ownership_or_admin(as_user, job):
    """A job added by somebody else must not be previewable -- the report
    names the series and how many episodes are missing."""
    resp = as_user("user").post("/api/autosync/%d/dry-run" % job["id"])
    assert resp.status_code == 403


def test_endpoint_returns_the_report(as_user, job):
    resp = as_user("admin").post("/api/autosync/%d/dry-run" % job["id"])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["job_id"] == job["id"]
    assert body["dry_run"] is True


def test_endpoint_404s_for_an_unknown_job(as_user):
    assert as_user("admin").post("/api/autosync/999999/dry-run").status_code == 404


# ==========================================================================
# test_syncplay_social.py
#
# Watch-party reactions and invite links.
# 
# The reaction half is a regression test with a story: the reaction bar has been
# in templates/syncplay.html since the room UI was written, posting to
# ``/api/syncplay/reaction`` — a route that did not exist. Every tap was a silent
# 404, and nothing in the UI said so, because a reaction that does not arrive
# looks exactly like a reaction nobody else sent.
# ==========================================================================
@pytest.fixture()
def room():
    token, _room, _snap = sp.join("pytest-party", "Host", is_guest=False)
    yield token
    try:
        sp.close_room(token)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------

def test_the_emoji_set_matches_the_buttons_in_the_template():
    """The bar and the server have to agree, or a tap is a silent no-op."""
    import pathlib
    import re

    template = (pathlib.Path(__file__).resolve().parents[1] / "src" / "mediaforge"
                / "web" / "templates" / "syncplay.html").read_text(encoding="utf-8")
    in_template = set(re.findall(r"SP\.react\('([^']+)'\)", template))
    assert in_template, "the reaction bar disappeared from the template"
    missing = in_template - set(sp.REACTIONS)
    assert not missing, "buttons the server would reject: %s" % missing


def test_a_reaction_reaches_the_room(room):
    queue = sp.subscribe(room)
    assert queue is not None
    assert sp.react(room, sp.REACTIONS[0]) is True

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    kinds = [e.get("type") for e in events]
    assert "reaction" in kinds
    reaction = [e for e in events if e.get("type") == "reaction"][0]
    assert reaction["emoji"] == sp.REACTIONS[0]
    assert reaction["name"] == "Host"


def test_free_text_is_refused(room):
    """An arbitrary string is rendered straight into everyone's player."""
    assert sp.react(room, "<img src=x onerror=alert(1)>") is False
    assert sp.react(room, "") is False
    assert sp.react(room, "not-an-emoji") is False


def test_reactions_are_not_chat(room):
    """A reaction is a moment, not a record. Twenty people tapping at a
    cliffhanger must not push the conversation out of the chat history."""
    for _ in range(5):
        sp.react(room, sp.REACTIONS[1])
    found = sp.room_for_token(room)
    assert found is not None
    assert len(found.chat) == 0


def test_rate_limited_per_member(room):
    ok = sum(1 for _ in range(sp.REACTION_BURST + 5) if sp.react(room, sp.REACTIONS[2]))
    assert ok == sp.REACTION_BURST

    # A second member has their own budget: one enthusiastic person must not
    # be able to silence everybody else.
    other, _r, _s = sp.join("pytest-party", "Guest", is_guest=True)
    assert sp.react(other, sp.REACTIONS[2]) is True


def test_unknown_token_cannot_react():
    assert sp.react("not-a-token", sp.REACTIONS[0]) is False


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

def test_host_can_invite_and_the_code_resolves(room):
    invite = sp.create_invite(room, minutes=30)
    assert invite and invite["room"] == "pytest-party"
    assert sp.resolve_invite(invite["code"]) == "pytest-party"


def test_guests_cannot_create_invites(room):
    """A guest handing out further invites is how a private room stops being
    one."""
    guest, _r, _s = sp.join("pytest-party", "Guest2", is_guest=True)
    assert sp.create_invite(guest) is None


def test_an_expired_invite_stops_resolving(room, monkeypatch):
    invite = sp.create_invite(room, minutes=1)
    assert sp.resolve_invite(invite["code"]) == "pytest-party"

    real_time = time.time
    monkeypatch.setattr(sp.time, "time", lambda: real_time() + 3600)
    assert sp.resolve_invite(invite["code"]) is None


def test_a_limited_invite_runs_out(room):
    invite = sp.create_invite(room, minutes=60, uses=2)
    assert invite["uses_left"] == 2
    assert sp.consume_invite(invite["code"]) is True
    assert sp.consume_invite(invite["code"]) is True
    # Spent: resolving must stop working.
    assert sp.resolve_invite(invite["code"]) is None
    assert sp.consume_invite(invite["code"]) is False


def test_resolving_does_not_consume(room):
    """Resolving happens on page load. Burning the invite there would spend it
    on a refresh, or on the visitor being bounced by a password prompt."""
    invite = sp.create_invite(room, minutes=60, uses=1)
    for _ in range(5):
        assert sp.resolve_invite(invite["code"]) == "pytest-party"
    assert sp.consume_invite(invite["code"]) is True
    assert sp.resolve_invite(invite["code"]) is None


def test_revoking_kills_a_link_immediately(room):
    invite = sp.create_invite(room, minutes=60)
    assert sp.revoke_invite(room, invite["code"]) is True
    assert sp.resolve_invite(invite["code"]) is None
    assert sp.revoke_invite(room, invite["code"]) is False


def test_live_invites_are_bounded(room):
    for _ in range(sp.MAX_INVITES):
        assert sp.create_invite(room, minutes=60) is not None
    assert sp.create_invite(room, minutes=60) is None


def test_lifetime_is_clamped(room):
    """A "1 minute" and a "ten years" invite are both configuration mistakes."""
    short = sp.create_invite(room, minutes=0)
    assert short["expires_at"] > time.time()
    long_lived = sp.create_invite(room, minutes=99999999)
    assert long_lived["expires_at"] < time.time() + 60 * 60 * 24 * 8


def test_unknown_code_resolves_to_nothing():
    assert sp.resolve_invite("nope") is None
    assert sp.resolve_invite("") is None
