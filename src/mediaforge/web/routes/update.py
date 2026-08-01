"""Update-check / self-update routes + workers.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from .. import selfupdate
from ..db import get_setting
from ..db import set_setting
from ..version_info import _UPDATE_CHECK_INTERVAL
from ..version_info import _get_display_version
from ..version_info import _do_update_check
from ..version_info import _update_cache
from flask import jsonify
from flask import request
import os
import threading
from ..request_context import get_current_user_info as _get_current_user_info
from ...logger import get_logger


logger = get_logger(__name__)


def _update_check_worker():
    import time as _time
    _time.sleep(10)
    while True:
        try:
            _do_update_check()
            logger.info(
                "[UpdateCheck] Latest: %s | update_available: %s",
                _update_cache["latest_version"],
                _update_cache["update_available"],
            )
        except Exception:
            logger.exception("[UpdateCheck] Unexpected error")
        _time.sleep(_UPDATE_CHECK_INTERVAL)


def _auto_update_worker():
    import time as _t
    from datetime import datetime as _dt
    _t.sleep(20)
    while True:
        try:
            if get_setting("auto_update_enabled", "0") == "1":
                inst = selfupdate.detect_install()
                # Deliberately can_self_update, NOT can_code_update: the
                # Docker code update is a manual, confirmed action only. It
                # is lost on the next image pull and it changes the code
                # under a running container, so nothing here may trigger it
                # unattended in the middle of the night.
                if inst["can_self_update"]:
                    now = _dt.now()
                    days_raw = get_setting("auto_update_days", "0,1,2,3,4,5,6") or ""
                    day_ok = str(now.weekday()) in [d.strip() for d in days_raw.split(",") if d.strip() != ""]
                    target_time = (get_setting("auto_update_time", "03:00") or "03:00").strip()
                    now_hhmm = now.strftime("%H:%M")
                    today = now.strftime("%Y-%m-%d")
                    already = get_setting("auto_update_last_run", "") == today
                    if day_ok and now_hhmm == target_time and not already:
                        set_setting("auto_update_last_run", today)
                        if (_t.time() - _update_cache["checked_at"]) > 300:
                            _do_update_check()
                        if _update_cache["update_available"]:
                            logger.info("[AutoUpdate] scheduled update starting")
                            try:
                                selfupdate.start_update()
                                from ..db import get_db as _gd
                                _c = _gd()
                                try:
                                    _c.execute("UPDATE download_queue SET status='queued' WHERE status='running'")
                                    _c.commit()
                                finally:
                                    _c.close()
                                _t.sleep(1.5)
                                os._exit(0)
                            except selfupdate.UpdateError as _ue:
                                logger.warning("[AutoUpdate] cannot update: %s", _ue)
                        else:
                            logger.info("[AutoUpdate] no update available, skipping")
        except Exception:
            logger.exception("[AutoUpdate] worker error")
        _t.sleep(30)


def ensure_update_check_worker():
    """Start the periodic update-check worker thread."""
    _uct = threading.Thread(target=_update_check_worker, daemon=True, name="update-checker")
    _uct.start()


def ensure_auto_update_worker():
    """Start the scheduled auto-update worker thread."""
    _aut = threading.Thread(target=_auto_update_worker, daemon=True, name="auto-update")
    _aut.start()


def _resolve_docker_target():
    """Return ``(version, error)`` for a Docker code update.

    The target version is resolved **server-side** from the update cache and
    is never read from the request body: a client may ask *whether* to
    update, never *what* to update to. Anything else would let an
    authenticated admin (or anything that got hold of a session) point the
    container at an arbitrary package version.
    """
    import time as _time
    if (_time.time() - _update_cache["checked_at"]) > _UPDATE_CHECK_INTERVAL:
        _do_update_check()
    if not _update_cache.get("update_available"):
        return None, "no update available"
    version = _update_cache.get("latest_version")
    if not selfupdate.is_valid_target_version(version):
        # On a dev install this cache holds a 7-character commit SHA rather than
        # a version, which lands here as well -- a container is never pointed at
        # a git revision.
        return None, "the latest published version could not be determined"
    if not selfupdate.is_stable_target_version(version):
        return None, (f"'{version}' is a pre-release; containers install final "
                      f"releases only")
    return version, None


def register_update_routes(app):
    @app.route("/api/update-check", methods=["GET", "POST"])
    def api_update_check():
        import time
        data = request.get_json(silent=True) or {}
        force = data.get("force", False)
        stale = (time.time() - _update_cache["checked_at"]) > _UPDATE_CHECK_INTERVAL
        if force or stale:
            _do_update_check()
        _inst = selfupdate.detect_install()
        return jsonify({
            "local_version": _get_display_version(),
            "latest_version": _update_cache["latest_version"],
            "update_available": _update_cache["update_available"],
            "release_url": _update_cache["release_url"],
            "release_notes": _update_cache["release_notes"],
            "checked_at": _update_cache["checked_at"],
            "error": _update_cache["error"],
            "is_dev_install": _update_cache["is_dev_install"],
            "install_type": _inst["type"],
            "channel": _inst["channel"],
            "can_self_update": _inst["can_self_update"],
            # Docker: the package inside the container can be swapped even
            # though a full self-update is not possible — see
            # selfupdate.detect_install() for why these are two flags.
            "can_code_update": _inst.get("can_code_update", False),
            "code_update_reason": _inst.get("code_update_reason"),
            "is_docker": _inst["type"] == "docker",
        })
    @app.route("/api/update/install", methods=["POST"])
    def api_update_install():
        _u, _is_admin = _get_current_user_info()
        if not _is_admin:
            return jsonify({"error": "forbidden"}), 403
        data = request.get_json(silent=True) or {}
        channel = data.get("channel")
        if channel is not None:
            channel = str(channel).strip().lower()
            if channel not in ("stable", "dev"):
                return jsonify({"error": "invalid channel"}), 400

        _inst = selfupdate.detect_install()
        if _inst["type"] == "docker":
            # Container: swap only the mediaforge package, stable channel only.
            # The preflight runs again here on purpose -- the UI already ran
            # it, but a UI check is a convenience, never the safeguard. It is
            # served from selfupdate's 60s cache, so this costs nothing.
            # Two separate refusals on purpose. The first covers an explicit
            # "install dev" request; the second covers a container that simply
            # *is* a dev install and sends no channel at all, which would
            # otherwise fall through to the target resolution below.
            if channel == "dev" or _inst.get("channel") == "dev":
                return jsonify({
                    "error": "the dev channel cannot be installed inside a container",
                    "reason": "docker_dev_channel",
                }), 409
            if not _inst.get("can_code_update"):
                return jsonify({
                    "error": "code update not available for this container",
                    "reason": _inst.get("code_update_reason"),
                }), 409
            version, err = _resolve_docker_target()
            if err:
                return jsonify({"error": err}), 409
            try:
                result = selfupdate.start_docker_code_update(version)
            except selfupdate.UpdateError as exc:
                return jsonify({
                    "error": str(exc),
                    "reason": getattr(exc, "reason", None),
                    "blocking": getattr(exc, "blocking", []),
                }), 409
            except Exception as exc:
                logger.exception("[DockerUpdate] start failed")
                return jsonify({"error": str(exc)}), 500

            # Requeue in-flight downloads so they resume after the in-place
            # restart (the worker repeats this right before exec'ing, since
            # the install itself takes a few seconds).
            try:
                from ..db import get_db
                _c = get_db()
                try:
                    _c.execute("UPDATE download_queue SET status = 'queued' WHERE status = 'running'")
                    _c.commit()
                finally:
                    _c.close()
            except Exception:
                logger.warning("[DockerUpdate] could not pause download queue", exc_info=True)

            # No os._exit() here: the background worker installs and then
            # replaces this process with os.execv() itself. Exiting would kill
            # PID 1 and take the whole container down with it.
            return jsonify(result)

        try:
            result = selfupdate.start_update(target_channel=channel)
        except selfupdate.UpdateError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:
            logger.exception("[SelfUpdate] start failed")
            return jsonify({"error": str(exc)}), 500

        # Pause running downloads so they resume after the restart.
        try:
            from ..db import get_db
            _c = get_db()
            try:
                _c.execute("UPDATE download_queue SET status = 'queued' WHERE status = 'running'")
                _c.commit()
            finally:
                _c.close()
        except Exception:
            logger.warning("[SelfUpdate] could not pause download queue", exc_info=True)

        # Flush the response, then exit so the helper can replace files & relaunch.
        def _exit_soon():
            import time as _t
            _t.sleep(1.5)
            logger.info("[SelfUpdate] exiting for update helper")
            os._exit(0)
        threading.Thread(target=_exit_soon, daemon=True, name="selfupdate-exit").start()
        return jsonify(result)
    @app.route("/api/update/docker-preflight", methods=["POST"])
    def api_update_docker_preflight():
        """Serve POST /api/update/docker-preflight: check whether the newest
        release can be installed into this container without pulling in new
        dependencies.

        Admin-only, exactly like /api/update/install -- the check goes out to
        PyPI, so it must not be reachable by every logged-in user. The result
        itself is cached inside selfupdate for 60s per target version, which
        keeps repeated clicks from turning into repeated outbound requests.
        """
        _u, _is_admin = _get_current_user_info()
        if not _is_admin:
            return jsonify({"error": "forbidden"}), 403
        _inst = selfupdate.detect_install()
        if _inst["type"] != "docker":
            return jsonify({"error": "not a docker installation"}), 409
        if not _inst.get("can_code_update"):
            return jsonify({
                "error": "code update not available for this container",
                "reason": _inst.get("code_update_reason"),
            }), 409
        version, err = _resolve_docker_target()
        if err:
            return jsonify({"error": err}), 409
        try:
            result = selfupdate.docker_update_preflight(version)
        except Exception as exc:
            logger.exception("[DockerUpdate] preflight failed")
            return jsonify({"error": str(exc)}), 500
        result["target_version"] = version
        return jsonify(result)
    @app.route("/api/update/status", methods=["GET"])
    def api_update_status():
        return jsonify(selfupdate.read_status())
    @app.route("/api/update/status/ack", methods=["POST"])
    def api_update_status_ack():
        selfupdate.ack_status()
        return jsonify({"ok": True})
