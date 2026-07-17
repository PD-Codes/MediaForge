"""Uptime monitor routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ..db import get_setting
from ..db import get_uptime_heartbeats_between
from ..db import get_uptime_range
from ..db import set_setting
from ..uptime_monitor import _MONITOR_SITES
from ..uptime_monitor import _uptime_config
from ..uptime_monitor import _uptime_run_round
from ..uptime_monitor import _uptime_wake
from flask import jsonify
from flask import render_template
from flask import request
import threading
from ..request_context import get_current_user_info as _get_current_user_info


def register_uptime_routes(app):
    """Register the UpTime dashboard page and its supporting API routes
    (status/history, heartbeat detail, settings, manual check) on the
    Flask app."""
    @app.route("/uptime")
    def uptime_page():
        """Dedicated UpTime monitoring dashboard (visible only when enabled).
        GET /uptime."""
        from ..db import get_setting as _gs
        if _gs("uptime_enabled", "0") != "1":
            from flask import redirect, url_for
            return redirect(url_for("index"))
        return render_template("uptime.html")
    @app.route("/api/uptime/status")
    def api_uptime_status():
        """UpTime config + per-source stats and bucketed history over a window.

        Window selection (query params):
          range=<seconds>  -> [now-range, now]
          start=&end=      -> explicit epoch-second window (custom range)
        The window is clamped to the retention period (older data is pruned).

        GET /api/uptime/status. Called from static/app.js's
        `applyUptimeStatus()`, static/integrations.js's `loadUptimeSettings()`,
        and static/uptime.js's `refresh()` (via its `statusUrl()` helper)."""
        import time as _t
        cfg = _uptime_config()
        now = int(_t.time())
        retention_sec = cfg["retention_days"] * 86400
        oldest = now - retention_sec

        def _int(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        start = _int(request.args.get("start"))
        end = _int(request.args.get("end"))
        rng = _int(request.args.get("range"))
        if start is not None or end is not None:
            end = end if end is not None else now
            start = start if start is not None else (end - 3600)
        else:
            if rng is None or rng <= 0:
                rng = min(6 * 3600, retention_sec)
            rng = max(300, min(rng, retention_sec))
            end = now
            start = now - rng
        # Clamp to sane bounds (allow custom start before retention -> shows nodata)
        if end > now:
            end = now
        if start >= end:
            start = end - 300
        # Don't scan absurdly far before pruned data, but keep the user's window
        # so missing parts render as "no data".
        if start < oldest - retention_sec:
            start = oldest - retention_sec

        n_buckets = 50
        sources = []
        for _sid, (_label, _url, _domain, _markers, _headers) in _MONITOR_SITES.items():
            rr = get_uptime_range(_sid, start, end, n_buckets=n_buckets)
            latest = rr["latest"] or {}
            _src_def = "0" if _sid == "hanime" else "1"
            sources.append({
                "id":               _sid,
                "label":            _label,
                "url":              _url,
                "tracked":          cfg["tracked"].get(_sid, False),
                "enabled_source":   get_setting("source_enabled_" + _sid, _src_def) == "1",
                "current_status":   latest.get("status"),
                "last_ts":          latest.get("ts"),
                "last_response_ms": latest.get("response_ms"),
                "last_http_status": latest.get("http_status"),
                "last_message":     latest.get("message"),
                "blocked":          latest.get("status") == "down" and latest.get("message") == "blocked_page",
                "uptime_pct":       rr["stats"]["uptime_pct"],
                "avg_ms":           rr["stats"]["avg_ms"],
                "total_checks":     rr["stats"]["total"],
                "bucket_seconds":   rr["bucket_seconds"],
                "buckets":          rr["buckets"],
            })
        return jsonify({
            "enabled":           cfg["enabled"],
            "interval":          cfg["interval"],
            "retention_days":    cfg["retention_days"],
            "timeout":           cfg["timeout"],
            "failure_threshold": cfg["failure_threshold"],
            "use_get":           cfg["use_get"],
            "now":               now,
            "range_start":    start,
            "range_end":      end,
            "range_seconds":  end - start,
            "sources":        sources,
        })
    @app.route("/api/uptime/heartbeats")
    def api_uptime_heartbeats():
        """Raw heartbeats for one source within a time window (bucket detail).
        GET /api/uptime/heartbeats.

        Called from static/uptime.js's `openDetail()`."""
        import time as _t
        src = (request.args.get("source") or "").strip()
        if src not in _MONITOR_SITES:
            return jsonify({"error": "unknown source"}), 400
        now = int(_t.time())
        def _int(v, d):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return d
        start = _int(request.args.get("start"), now - 3600)
        end = _int(request.args.get("end"), now)
        rows = get_uptime_heartbeats_between(src, start, end, limit=1000)
        return jsonify({"source": src, "start": start, "end": end, "heartbeats": rows})
    @app.route("/api/settings/uptime", methods=["PUT"])
    def api_settings_uptime():
        """Update UpTime monitor settings (enabled, interval, retention,
        timeout, per-source tracking). Admin-only. PUT /api/settings/uptime.

        Called from static/integrations.js's `saveUptimeSettings()`."""
        _u, _is_admin = _get_current_user_info()
        if not _is_admin:
            return jsonify({"error": "forbidden"}), 403
        data = request.get_json(silent=True) or {}

        if "enabled" in data:
            on = str(data["enabled"]).lower() in ("1", "true", "on", "yes")
            set_setting("uptime_enabled", "1" if on else "0")

        def _save_int(key, db_key, lo, hi):
            if key not in data:
                return None
            try:
                v = int(float(data[key]))
            except (TypeError, ValueError):
                return "invalid " + key
            set_setting(db_key, str(max(lo, min(hi, v))))
            return None

        for _k, _dbk, _lo, _hi in (
            ("interval",          "uptime_interval",           60, 86400),
            ("retention_days",    "uptime_retention_days",      1,     7),
            ("timeout",           "uptime_timeout",             5,   120),
            ("failure_threshold", "uptime_failure_threshold",   1,    10),
        ):
            err = _save_int(_k, _dbk, _lo, _hi)
            if err:
                return jsonify({"error": err}), 400

        if "use_get" in data:
            on = str(data["use_get"]).lower() in ("1", "true", "on", "yes")
            set_setting("uptime_use_get", "1" if on else "0")

        tracked = data.get("tracked")
        if isinstance(tracked, dict):
            for _sid in _MONITOR_SITES:
                if _sid in tracked:
                    set_setting("uptime_track_" + _sid,
                                "1" if tracked[_sid] else "0")

        _uptime_wake.set()  # apply immediately (start/adjust the monitor)
        return jsonify({"ok": True})
    @app.route("/api/uptime/check-now", methods=["POST"])
    def api_uptime_check_now():
        """Trigger an immediate out-of-cycle uptime check round. Admin-only.
        POST /api/uptime/check-now.

        Called from static/uptime.js's `window.uptimeCheckNow()`."""
        _u, _is_admin = _get_current_user_info()
        if not _is_admin:
            return jsonify({"error": "forbidden"}), 403
        cfg = _uptime_config()
        if not cfg["enabled"]:
            return jsonify({"error": "uptime disabled"}), 400
        threading.Thread(target=_uptime_run_round, daemon=True,
                         name="uptime-checknow").start()
        return jsonify({"ok": True})
