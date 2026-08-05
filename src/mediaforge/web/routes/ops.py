"""Operations API: audit log, groups, schema/snapshots, workers, maintenance,
diagnostics, rules and language profiles.

All of it is admin-only. The endpoints are listed in app.py's ``_admin_only``
set like every other privileged route -- authorisation in this app lives in
that one hand-maintained set rather than on the individual routes, and adding
a route here without adding it there would leave it open to any logged-in
account (see tests/test_admin_gating.py, which asserts exactly that).

Extracted as a plain route-registration function (no Flask blueprint: endpoint
names stay bare so url_for() keeps working), matching every other routes
module here.
"""

import io

from flask import jsonify
from flask import request
from flask import send_file

from ...logger import get_logger
from ..request_context import get_current_user_info as _get_current_user_info

logger = get_logger(__name__)


def _require_admin():
    _user, is_admin = _get_current_user_info()
    if not is_admin:
        return jsonify({"error": "admin access required"}), 403
    return None


def _body():
    return request.get_json(silent=True) or {}


def register_ops_routes(app):
    """Register every /api/ops/* endpoint plus the audit and group APIs."""

    # -----------------------------------------------------------------
    # Health -- deliberately NOT admin-gated and NOT login-gated.
    # -----------------------------------------------------------------
    @app.route("/healthz")
    def healthz():
        """Liveness. Answers before login so Docker HEALTHCHECK, k8s and
        Uptime-Kuma can use it.

        It returns booleans and nothing else on purpose. A status endpoint that
        leaks version numbers, worker names or error strings to an unauthenticated
        caller is a reconnaissance endpoint; the detailed version lives behind
        the admin gate at /api/ops/workers.
        """
        return jsonify({"status": "ok"}), 200

    @app.route("/readyz")
    def readyz():
        """Readiness: the database answers and the schema is not mid-migration."""
        ready = True
        try:
            from ..db import get_db
            conn = get_db()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception:
            ready = False
        return jsonify({"status": "ok" if ready else "degraded"}), (200 if ready else 503)

    # -----------------------------------------------------------------
    # Audit log
    # -----------------------------------------------------------------
    @app.route("/api/ops/audit")
    def api_ops_audit():
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        return jsonify(_audit.query(
            category=request.args.get("category", ""),
            action=request.args.get("action", ""),
            severity=request.args.get("severity", ""),
            search=request.args.get("q", ""),
            since=request.args.get("since", ""),
            until=request.args.get("until", ""),
            limit=request.args.get("limit", 100),
            offset=request.args.get("offset", 0),
        ))

    @app.route("/api/ops/audit/stats")
    def api_ops_audit_stats():
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        return jsonify({"stats": _audit.stats(),
                        "categories": list(_audit.CATEGORIES),
                        "severities": list(_audit.SEVERITIES)})

    @app.route("/api/ops/audit/verify")
    def api_ops_audit_verify():
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        return jsonify(_audit.verify_chain())

    @app.route("/api/ops/audit/export")
    def api_ops_audit_export():
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        csv_text = _audit.export_csv(
            category=request.args.get("category", ""),
            search=request.args.get("q", ""),
            since=request.args.get("since", ""),
            until=request.args.get("until", ""),
        )
        _audit.audit("system", "audit_exported",
                     detail={"bytes": len(csv_text)}, severity="notice")
        return send_file(io.BytesIO(csv_text.encode("utf-8")),
                         mimetype="text/csv", as_attachment=True,
                         download_name="mediaforge-audit.csv")

    # -----------------------------------------------------------------
    # Groups and permissions
    # -----------------------------------------------------------------
    @app.route("/api/ops/groups")
    def api_ops_groups():
        guard = _require_admin()
        if guard:
            return guard
        from ..groups import PERMISSIONS, list_groups
        return jsonify({"groups": list_groups(), "permissions": PERMISSIONS})

    @app.route("/api/ops/groups", methods=["POST"])
    def api_ops_group_create():
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from ..groups import create_group
        data = _body()
        gid, err = create_group(
            data.get("key", ""), data.get("name", ""),
            data.get("permissions"), data.get("scope"),
            data.get("description", ""))
        if err:
            return jsonify({"error": err}), 400
        _audit.audit("group", "group_created", target=data.get("name", ""),
                     detail={"id": gid, "permissions": data.get("permissions"),
                             "scope": data.get("scope")}, severity="notice")
        return jsonify({"ok": True, "id": gid})

    @app.route("/api/ops/groups/<int:group_id>", methods=["PUT"])
    def api_ops_group_update(group_id):
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from ..groups import get_group, update_group
        before = get_group(group_id)
        data = _body()
        ok, err = update_group(
            group_id, name=data.get("name"), description=data.get("description"),
            permissions=data.get("permissions"), scope=data.get("scope"))
        if not ok:
            return jsonify({"error": err}), 400
        _audit.audit("group", "group_updated",
                     target=(before or {}).get("name") or str(group_id),
                     detail={"before": before, "after": get_group(group_id)},
                     severity="notice")
        return jsonify({"ok": True})

    @app.route("/api/ops/groups/<int:group_id>", methods=["DELETE"])
    def api_ops_group_delete(group_id):
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from ..groups import delete_group, get_group
        before = get_group(group_id)
        ok, err = delete_group(group_id)
        if not ok:
            return jsonify({"error": err}), 400
        _audit.audit("group", "group_deleted",
                     target=(before or {}).get("name") or str(group_id),
                     detail={"id": group_id}, severity="warning")
        return jsonify({"ok": True})

    # -----------------------------------------------------------------
    # Schema version, snapshots, rollback
    # -----------------------------------------------------------------
    @app.route("/api/ops/schema")
    def api_ops_schema():
        guard = _require_admin()
        if guard:
            return guard
        from .. import dbmigrate
        return jsonify({"migrations": dbmigrate.status(),
                        "snapshots": dbmigrate.list_snapshots()})

    @app.route("/api/ops/snapshots", methods=["POST"])
    def api_ops_snapshot_create():
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from .. import dbmigrate
        meta = dbmigrate.snapshot(reason="manual", note=_body().get("note", "")[:200])
        if not meta:
            return jsonify({"error": "no_database"}), 400
        _audit.audit("backup", "snapshot_created", target=meta["id"],
                     detail={"size": meta["size"]}, severity="notice")
        return jsonify({"ok": True, "snapshot": meta})

    @app.route("/api/ops/snapshots/<snapshot_id>/verify")
    def api_ops_snapshot_verify(snapshot_id):
        guard = _require_admin()
        if guard:
            return guard
        from .. import dbmigrate
        result = dbmigrate.verify_snapshot(snapshot_id)
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.route("/api/ops/snapshots/<snapshot_id>/restore", methods=["POST"])
    def api_ops_snapshot_restore(snapshot_id):
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from .. import dbmigrate
        # Recorded BEFORE the restore, and flushed: the audit log lives in its
        # own database precisely so this entry survives the main database being
        # replaced under it, but the write still has to happen while the app is
        # in a known state.
        _audit.audit("backup", "snapshot_restore_started", target=snapshot_id,
                     severity="critical")
        _audit.flush(3.0)
        result = dbmigrate.restore_snapshot(snapshot_id)
        _audit.audit("backup", "snapshot_restored", target=snapshot_id,
                     outcome="success" if result.get("ok") else "failure",
                     severity="critical", detail=result)
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.route("/api/ops/snapshots/<snapshot_id>", methods=["DELETE"])
    def api_ops_snapshot_delete(snapshot_id):
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from .. import dbmigrate
        ok = dbmigrate.delete_snapshot(snapshot_id)
        if ok:
            _audit.audit("backup", "snapshot_deleted", target=snapshot_id,
                         severity="warning")
        return jsonify({"ok": ok}), (200 if ok else 404)

    # -----------------------------------------------------------------
    # Workers
    # -----------------------------------------------------------------
    @app.route("/api/ops/workers")
    def api_ops_workers():
        guard = _require_admin()
        if guard:
            return guard
        from ..worker_registry import health, snapshot
        return jsonify({"workers": snapshot(), "health": health()})

    # -----------------------------------------------------------------
    # Maintenance windows
    # -----------------------------------------------------------------
    @app.route("/api/ops/maintenance")
    def api_ops_maintenance():
        guard = _require_admin()
        if guard:
            return guard
        from .. import maintenance
        return jsonify({"windows": maintenance.list_windows(),
                        "current": maintenance.current_limits()})

    @app.route("/api/ops/maintenance", methods=["POST"])
    def api_ops_maintenance_create():
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from .. import maintenance
        wid, err = maintenance.create_window(_body())
        if err:
            return jsonify({"error": err}), 400
        _audit.audit("settings", "maintenance_window_created",
                     target=_body().get("name", ""), detail=_body())
        return jsonify({"ok": True, "id": wid})

    @app.route("/api/ops/maintenance/<int:window_id>", methods=["PUT"])
    def api_ops_maintenance_update(window_id):
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from .. import maintenance
        ok, err = maintenance.update_window(window_id, _body())
        if err:
            return jsonify({"error": err}), 400
        _audit.audit("settings", "maintenance_window_updated",
                     target=str(window_id), detail=_body())
        return jsonify({"ok": ok})

    @app.route("/api/ops/maintenance/<int:window_id>", methods=["DELETE"])
    def api_ops_maintenance_delete(window_id):
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from .. import maintenance
        ok = maintenance.delete_window(window_id)
        if ok:
            _audit.audit("settings", "maintenance_window_deleted", target=str(window_id))
        return jsonify({"ok": ok}), (200 if ok else 404)

    # -----------------------------------------------------------------
    # Diagnostics bundle
    # -----------------------------------------------------------------
    @app.route("/api/ops/diagnostics")
    def api_ops_diagnostics():
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from .. import diagnostics
        try:
            payload, filename = diagnostics.build_bundle()
        except Exception as exc:
            logger.error("[Diagnostics] Bundle failed: %s", exc, exc_info=True)
            return jsonify({"error": str(exc)}), 500
        _audit.audit("system", "diagnostics_exported", target=filename,
                     detail={"bytes": len(payload)}, severity="notice")
        return send_file(io.BytesIO(payload), mimetype="application/zip",
                         as_attachment=True, download_name=filename)

    # -----------------------------------------------------------------
    # Rules
    # -----------------------------------------------------------------
    @app.route("/api/ops/rules")
    def api_ops_rules():
        guard = _require_admin()
        if guard:
            return guard
        from ..rules import ACTIONS, FIELDS, OPERATORS, list_rules
        return jsonify({
            "rules": list_rules(),
            "fields": FIELDS,
            "operators": OPERATORS,
            "actions": {k: v[0] for k, v in ACTIONS.items()},
        })

    @app.route("/api/ops/rules", methods=["POST"])
    def api_ops_rule_create():
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from ..rules import save_rule
        rid, err = save_rule(_body())
        if err:
            return jsonify({"error": err}), 400
        _audit.audit("settings", "rule_created", target=_body().get("name", ""),
                     detail=_body())
        return jsonify({"ok": True, "id": rid})

    @app.route("/api/ops/rules/<int:rule_id>", methods=["PUT"])
    def api_ops_rule_update(rule_id):
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from ..rules import get_rule, save_rule
        before = get_rule(rule_id)
        _rid, err = save_rule(_body(), rule_id)
        if err:
            return jsonify({"error": err}), 400
        _audit.audit("settings", "rule_updated",
                     target=(before or {}).get("name") or str(rule_id),
                     detail={"before": before, "after": get_rule(rule_id)})
        return jsonify({"ok": True})

    @app.route("/api/ops/rules/<int:rule_id>", methods=["DELETE"])
    def api_ops_rule_delete(rule_id):
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from ..rules import delete_rule, get_rule
        before = get_rule(rule_id)
        ok = delete_rule(rule_id)
        if ok:
            _audit.audit("settings", "rule_deleted",
                         target=(before or {}).get("name") or str(rule_id))
        return jsonify({"ok": ok}), (200 if ok else 404)

    @app.route("/api/ops/rules/test", methods=["POST"])
    def api_ops_rules_test():
        """Evaluate the saved rules against a hypothetical download.

        This is the rule editor's preview: it answers "given this title from
        this provider, what would actually happen", including which rules
        matched and in what order.
        """
        guard = _require_admin()
        if guard:
            return guard
        from ..rules import evaluate
        return jsonify({"result": evaluate(_body().get("context") or {})})

    # -----------------------------------------------------------------
    # Language profiles
    # -----------------------------------------------------------------
    @app.route("/api/ops/language-profiles")
    def api_ops_language_profiles():
        guard = _require_admin()
        if guard:
            return guard
        from ..langprofiles import list_bindings, list_profiles
        return jsonify({"profiles": list_profiles(), "bindings": list_bindings()})

    @app.route("/api/ops/language-profiles", methods=["POST"])
    def api_ops_language_profile_create():
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from ..langprofiles import save_profile
        pid, err = save_profile(_body())
        if err:
            return jsonify({"error": err}), 400
        _audit.audit("settings", "language_profile_created",
                     target=_body().get("name", ""), detail=_body())
        return jsonify({"ok": True, "id": pid})

    @app.route("/api/ops/language-profiles/<int:profile_id>", methods=["PUT"])
    def api_ops_language_profile_update(profile_id):
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from ..langprofiles import save_profile
        _pid, err = save_profile(_body(), profile_id)
        if err:
            return jsonify({"error": err}), 400
        _audit.audit("settings", "language_profile_updated",
                     target=str(profile_id), detail=_body())
        return jsonify({"ok": True})

    @app.route("/api/ops/language-profiles/<int:profile_id>", methods=["DELETE"])
    def api_ops_language_profile_delete(profile_id):
        guard = _require_admin()
        if guard:
            return guard
        from .. import audit as _audit
        from ..langprofiles import delete_profile
        ok = delete_profile(profile_id)
        if ok:
            _audit.audit("settings", "language_profile_deleted", target=str(profile_id))
        return jsonify({"ok": ok}), (200 if ok else 404)

    @app.route("/api/ops/language-profiles/bind", methods=["POST"])
    def api_ops_language_profile_bind():
        guard = _require_admin()
        if guard:
            return guard
        from ..langprofiles import bind_title
        data = _body()
        profile_id = data.get("profile_id")
        ok = bind_title(data.get("series_url", ""),
                        None if profile_id in (None, "", 0) else int(profile_id),
                        data.get("title", ""))
        return jsonify({"ok": ok}), (200 if ok else 400)

    logger.debug("[Ops] Operations routes registered")


# Endpoint names this module registers that must be admin-only. app.py imports
# this and folds it into _admin_only, so the list cannot drift out of sync with
# the routes above the way a hand-copied one would.
ADMIN_ONLY_OPS_ENDPOINTS = frozenset({
    "api_ops_audit", "api_ops_audit_stats", "api_ops_audit_verify",
    "api_ops_audit_export",
    "api_ops_groups", "api_ops_group_create", "api_ops_group_update",
    "api_ops_group_delete",
    "api_ops_schema", "api_ops_snapshot_create", "api_ops_snapshot_verify",
    "api_ops_snapshot_restore", "api_ops_snapshot_delete",
    "api_ops_workers",
    "api_ops_maintenance", "api_ops_maintenance_create",
    "api_ops_maintenance_update", "api_ops_maintenance_delete",
    "api_ops_diagnostics",
    "api_ops_rules", "api_ops_rule_create", "api_ops_rule_update",
    "api_ops_rule_delete", "api_ops_rules_test",
    "api_ops_language_profiles", "api_ops_language_profile_create",
    "api_ops_language_profile_update", "api_ops_language_profile_delete",
    "api_ops_language_profile_bind",
})
