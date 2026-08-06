"""Operations layer: migrations, snapshots, audit log, groups, rules,
language profiles and maintenance windows.

These cover the behaviour that is easy to break silently and expensive to
notice late -- an audit row that can be deleted, a group whose scope is
ignored, a rule that matches when it should not -- rather than the CRUD
plumbing, which the route smoke test already exercises.
"""

import json

import pytest


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
