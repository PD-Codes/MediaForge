"""Upscale queue + settings routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).
"""

from ..db import add_to_upscale_queue
from ..db import cancel_upscale_item
from ..db import clear_upscale_completed
from ..db import get_setting
from ..db import get_upscale_badge_count
from ..db import get_upscale_queue
from ..db import move_upscale_queue_item
from ..db import remove_from_upscale_queue
from ..db import set_setting
from .library import lib_resolve_library_file
from ..runtime_state import _upscale_active_cancel_events
from ..runtime_state import _upscale_cancel_lock
from flask import jsonify
from flask import request
import threading
from ...logger import get_logger


logger = get_logger(__name__)


def register_upscale_routes(app):
    """Register the Anime4K upscale queue, per-item control and settings endpoints."""
    @app.route("/api/upscale/queue")
    def api_upscale_queue():
        """Return all upscale queue items plus the badge count.

        GET /api/upscale/queue. Called from upscale_queue.js's
        loadUpscaleQueue() to render the upscale queue page.
        """
        items = get_upscale_queue()
        badge = get_upscale_badge_count()
        return jsonify({"ok": True, "items": items, "badge": badge})
    @app.route("/api/upscale/progress")
    def api_upscale_progress():
        """Return the progress of the currently running upscale job, if any.

        GET /api/upscale/progress. Called from upscale_queue.js's
        loadUpscaleQueue().
        """
        try:
            from ...anime4k.anime4k import get_upscale_progress
            return jsonify({"ok": True, "progress": get_upscale_progress()})
        except Exception:
            return jsonify({"ok": True, "progress": {"active": False, "percent": 0}})
    @app.route("/api/upscale/badge")
    def api_upscale_badge():
        """Return just the upscale queue badge count (pending + running items).

        GET /api/upscale/badge. Polled from upscale_queue.js and library.js
        (libUpscaleTitle()/libUpscaleEpisode()) to refresh the nav badge
        after queuing new upscale jobs.
        """
        return jsonify({"ok": True, "count": get_upscale_badge_count()})
    @app.route("/api/upscale/queue/<int:item_id>", methods=["DELETE"])
    def api_upscale_queue_delete(item_id):
        """Remove a single item from the upscale queue.

        DELETE /api/upscale/queue/<item_id>. Called from upscale_queue.js's
        removeUpscaleItem().
        """
        ok, err = remove_from_upscale_queue(item_id)
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": err}), 400
    @app.route("/api/upscale/queue/<int:item_id>/cancel", methods=["POST"])
    def api_upscale_cancel(item_id):
        """Cancel a running or queued upscale job.

        POST /api/upscale/queue/<item_id>/cancel. Called from
        upscale_queue.js's cancelUpscaleItem().
        """
        ok, err = cancel_upscale_item(item_id)
        if ok:
            with _upscale_cancel_lock:
                ev = _upscale_active_cancel_events.get(item_id)
            if ev:
                ev.set()
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": err}), 400
    @app.route("/api/upscale/queue/clear", methods=["POST"])
    def api_upscale_clear():
        """Remove all completed items from the upscale queue.

        POST /api/upscale/queue/clear. Called from upscale_queue.js's
        clearUpscaleQueue().
        """
        clear_upscale_completed()
        return jsonify({"ok": True})
    @app.route("/api/upscale/queue/<int:item_id>/move", methods=["POST"])
    def api_upscale_move(item_id):
        """Move an upscale queue item up or down in the queue order.

        POST /api/upscale/queue/<item_id>/move. Called from
        upscale_queue.js's moveUpscaleItem(id, direction).
        """
        data = request.get_json(force=True, silent=True) or {}
        direction = data.get("direction", "up")
        ok, err = move_upscale_queue_item(item_id, direction)
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": err}), 400
    @app.route("/api/upscale/add-library", methods=["POST"])
    def api_upscale_add_library():
        """Add library files to the upscale queue as ONE batch entry.

        POST /api/upscale/add-library. Called from library.js's
        libUpscaleTitle() and libUpscaleEpisode() when the user upscales
        a title or single episode from the library view.
        """
        data = request.get_json(force=True) or {}
        files = data.get("files", [])  # list of {title, path}
        if not files:
            return jsonify({"ok": False, "error": "Keine Dateien angegeben"}), 400
        replace = get_setting("upscaling_replace_original", "1") == "1"
        valid = []
        title = None
        rejected = 0
        for f in files:
            # The client sends absolute paths, so every one of them has to be
            # checked against the library roots before it reaches the worker:
            # with replace_original on (the default) the worker overwrites
            # exactly this path. Existence alone is not a permission.
            fp = lib_resolve_library_file(f.get("path", ""))
            if fp is None:
                rejected += 1
                continue
            if replace:
                out = str(fp)
            else:
                out = str(fp.with_name(fp.stem + " (upscale).mkv"))
            valid.append({"file_path": str(fp), "output_path": out})
            if title is None:
                # Use the series title (strip episode suffix)
                t = f.get("title", fp.stem)
                title = t.split(" – ")[0].strip() if " – " in t else t
        if not valid:
            if rejected:
                logger.warning("[Upscale] %d Pfad(e) außerhalb der Mediathek abgelehnt", rejected)
                return jsonify({"ok": False, "error": "Datei liegt nicht in der Mediathek"}), 403
            return jsonify({"ok": False, "error": "Keine Dateien gefunden"}), 400
        add_to_upscale_queue(
            title=title or "Unbekannt",
            file_path=valid[0]["file_path"],
            output_path=valid[0]["output_path"],
            source="library",
            files=valid if len(valid) > 1 else None,
        )
        return jsonify({"ok": True, "added": len(valid)})
    @app.route("/api/upscale/settings", methods=["GET"])
    def api_upscale_settings_get():
        """Return the current global upscaling settings.

        GET /api/upscale/settings. Called from app.js (startup mode
        check), upscale_queue.js and templates/encoding.html's settings
        panel.
        """
        return jsonify({
            "ok": True,
            "settings": {
                "mode":            get_setting("upscaling_mode", "disabled"),
                "engine":          get_setting("upscaling_engine", "auto"),
                "shader_preset":   get_setting("upscaling_shader_preset", "B"),
                "shader_quality":  get_setting("upscaling_shader_quality", "high"),
                "resolution":      get_setting("upscaling_resolution", "1080p"),
                "replace_original":get_setting("upscaling_replace_original", "1"),
                "out_vcodec":      get_setting("upscaling_out_vcodec", "libx264"),
                "out_crf":         get_setting("upscaling_out_crf", "18"),
                "out_preset":      get_setting("upscaling_out_preset", "medium"),
            }
        })
    @app.route("/api/upscale/settings", methods=["POST"])
    def api_upscale_settings_post():
        """Persist the global upscaling settings, validating each field against its allowed set.

        POST /api/upscale/settings. Called from templates/encoding.html's
        settings-save handler.
        """
        data = request.get_json(force=True) or {}
        valid_modes    = ("disabled", "during_download", "after_download")
        valid_engines  = ("auto", "mpv", "libplacebo")
        valid_presets  = ("A", "B", "C", "D")
        valid_quality  = ("high", "low")
        valid_res      = ("1080p", "1440p", "4k", "source")
        valid_vcodec   = ("libx264", "libx265", "copy")
        valid_enc_pre  = ("ultrafast","superfast","veryfast","faster","fast",
                          "medium","slow","slower","veryslow")

        def _v(key, valid, default):
            val = data.get(key, default)
            return val if val in valid else default

        set_setting("upscaling_mode",             _v("mode", valid_modes, "disabled"))
        set_setting("upscaling_engine",           _v("engine", valid_engines, "auto"))
        set_setting("upscaling_shader_preset",    _v("shader_preset", valid_presets, "B"))
        set_setting("upscaling_shader_quality",   _v("shader_quality", valid_quality, "high"))
        set_setting("upscaling_resolution",       _v("resolution", valid_res, "1080p"))
        set_setting("upscaling_replace_original", "1" if data.get("replace_original", True) else "0")
        set_setting("upscaling_out_vcodec",       _v("out_vcodec", valid_vcodec, "libx264"))
        crf = str(max(0, min(51, int(data.get("out_crf", 18)))))
        set_setting("upscaling_out_crf",    crf)
        set_setting("upscaling_out_preset", _v("out_preset", valid_enc_pre, "medium"))
        return jsonify({"ok": True})
    @app.route("/api/upscale/mpv-status", methods=["GET"])
    def api_upscale_mpv_status():
        """Report whether mpv is available (bundled or on PATH) and its download status.

        GET /api/upscale/mpv-status. Called from templates/encoding.html
        to show mpv availability / download progress in the settings UI.
        """
        try:
            from ...autodeps import get_mpv_download_status, _bundled_mpv
            import shutil
            present = bool(_bundled_mpv() or shutil.which("mpv"))
            dl = get_mpv_download_status()
            return jsonify({"ok": True, "present": present, "download": dl})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    @app.route("/api/upscale/check-engines", methods=["POST"])
    def api_upscale_check_engines():
        """Detect available upscale engines (mpv/libplacebo) and shader availability.

        POST /api/upscale/check-engines. Called from templates/encoding.html
        when the user checks which engines/shaders are usable on this machine.
        """
        try:
            from ...anime4k.anime4k import get_available_engines, list_available_shaders
            engines  = get_available_engines()
            shaders  = list_available_shaders("high") + list_available_shaders("low")
            shaders  = sorted(set(shaders))
            return jsonify({"ok": True, "engines": engines, "shaders_available": len(shaders) > 0})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
    @app.route("/api/upscale/download-shaders", methods=["POST"])
    def api_upscale_download_shaders():
        """Download Anime4K GLSL shader pack in background.

        POST /api/upscale/download-shaders. No confirmed frontend caller
        found in static/ or templates/ at time of writing (shaders are
        currently bundled/fetched as part of check-engines/mpv-status
        flows rather than a dedicated UI button).
        """
        data     = request.get_json(force=True) or {}
        quality  = data.get("quality", "high")
        if quality not in ("high", "low"):
            return jsonify({"ok": False, "error": "quality must be high or low"}), 400

        def _dl():
            try:
                from ...anime4k.anime4k import download_anime4k, extract_anime4k
                files = download_anime4k(mode=quality)
                extract_anime4k(files)
                logger.info(f"[Anime4K] Shader-Download abgeschlossen ({quality})")
            except Exception as exc:
                logger.error(f"[Anime4K] Shader-Download fehlgeschlagen: {exc}")

        threading.Thread(target=_dl, daemon=True).start()
        return jsonify({"ok": True, "message": "Download gestartet"})
