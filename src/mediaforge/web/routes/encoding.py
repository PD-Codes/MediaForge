"""Encoding settings routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

# TODO(telemetry): wire up flag.transcoding / detail.transcoding (encoding
# errors) at the transcode call site -- see telemetry/registry.py.
# Registry-only for now.
"""

from ..db import cancel_encoding_item
from ..db import clear_encoding_completed
from ..db import get_encoding_badge_count
from ..db import get_encoding_queue
from ..db import get_setting
from ..db import move_encoding_queue_item
from ..db import remove_from_encoding_queue
from ..db import set_setting
from ..runtime_state import _encoding_active_cancel_events
from ..runtime_state import _encoding_cancel_lock
from flask import jsonify
from flask import render_template
from flask import request
import threading


_detect_hw_cache: "dict | None" = None
_detect_hw_cache_at: float = 0.0
_detect_hw_cache_ttl: float = 3600.0   # re-probe at most once per hour
_detect_hw_lock = threading.Lock()


def reset_detect_hw_cache():
    """Clear the cached HW-encoder probe result."""
    global _detect_hw_cache, _detect_hw_cache_at
    with _detect_hw_lock:
        _detect_hw_cache = None
        _detect_hw_cache_at = 0.0


def register_encoding_routes(app):
    @app.route("/encoding")
    def encoding_page():
        return render_template("encoding.html")
    @app.route("/api/encoding/settings", methods=["GET"])
    def api_encoding_settings_get():
        settings = {
            "mode":        get_setting("encoding_mode", "copy"),
            "audio_copy":  get_setting("encoding_audio_copy", "copy"),
            "hw_h264":     get_setting("encoding_hw_h264", "cpu"),
            "preset_h264": get_setting("encoding_preset_h264", "medium"),
            "crf_h264":    int(get_setting("encoding_crf_h264", "23") or "23"),
            "audio_h264":  get_setting("encoding_audio_h264", "copy"),
            "hw_h265":     get_setting("encoding_hw_h265", "cpu"),
            "preset_h265": get_setting("encoding_preset_h265", "medium"),
            "crf_h265":    int(get_setting("encoding_crf_h265", "28") or "28"),
            "audio_h265":  get_setting("encoding_audio_h265", "copy"),
            "expert_video": get_setting("encoding_expert_video", ""),
            "expert_audio": get_setting("encoding_expert_audio", ""),
            "vaapi_device": get_setting("encoding_vaapi_device", ""),
        }
        return jsonify({"ok": True, "settings": settings})
    @app.route("/api/encoding/settings", methods=["POST"])
    def api_encoding_settings_post():
        data = request.get_json(force=True) or {}
        mode = data.get("mode", "copy")
        valid_modes = ("copy", "h264", "h265", "expert")
        if mode not in valid_modes:
            return jsonify({"ok": False, "error": "Invalid mode"}), 400
        set_setting("encoding_mode", mode)
        if mode == "copy":
            audio = data.get("audio", "copy")
            if audio not in ("copy", "aac", "ac3"):
                audio = "copy"
            set_setting("encoding_audio_copy", audio)
        elif mode in ("h264", "h265"):
            hw = data.get("hw", "cpu")
            preset = data.get("preset", "medium")
            crf = str(int(data.get("crf", 23 if mode == "h264" else 28)))
            audio = data.get("audio", "copy")
            valid_presets = ("ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow")
            valid_hw = ("cpu","nvenc","vaapi","videotoolbox")
            valid_audio = ("copy","aac","ac3")
            if preset not in valid_presets: preset = "medium"
            if hw not in valid_hw: hw = "cpu"
            if audio not in valid_audio: audio = "copy"
            vaapi_device = data.get("vaapi_device", "")
            set_setting(f"encoding_hw_{mode}", hw)
            set_setting(f"encoding_preset_{mode}", preset)
            set_setting(f"encoding_crf_{mode}", crf)
            set_setting(f"encoding_audio_{mode}", audio)
            set_setting("encoding_vaapi_device", vaapi_device)
        elif mode == "expert":
            set_setting("encoding_expert_video", data.get("expert_video", ""))
            set_setting("encoding_expert_audio", data.get("expert_audio", ""))
        return jsonify({"ok": True})
    @app.route("/api/encoding/queue")
    def api_encoding_queue():
        """Return all encoding queue items plus the badge count.

        GET /api/encoding/queue. Called from encoding_queue.js's
        loadEncodingQueue() to render the encoding queue modal.
        """
        items = get_encoding_queue()
        badge = get_encoding_badge_count()
        return jsonify({"ok": True, "items": items, "badge": badge})
    @app.route("/api/encoding/queue/progress")
    def api_encoding_queue_progress():
        """Return the progress of the currently running encoding job, if any.

        GET /api/encoding/queue/progress. Called from encoding_queue.js's
        loadEncodingQueue().
        """
        try:
            from ..encoding_worker import get_encoding_progress
            return jsonify({"ok": True, "progress": get_encoding_progress()})
        except Exception:
            return jsonify({"ok": True, "progress": {"active": False, "percent": 0}})
    @app.route("/api/encoding/queue/badge")
    def api_encoding_queue_badge():
        """Return just the encoding queue badge count (pending + running items).

        GET /api/encoding/queue/badge. Polled from encoding_queue.js and
        base.html to refresh the nav badge.
        """
        return jsonify({"ok": True, "count": get_encoding_badge_count()})
    @app.route("/api/encoding/queue/<int:item_id>", methods=["DELETE"])
    def api_encoding_queue_delete(item_id):
        """Remove a single item from the encoding queue.

        DELETE /api/encoding/queue/<item_id>. Called from encoding_queue.js's
        removeEncodingItem().
        """
        ok, err = remove_from_encoding_queue(item_id)
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": err}), 400
    @app.route("/api/encoding/queue/<int:item_id>/cancel", methods=["POST"])
    def api_encoding_queue_cancel(item_id):
        """Cancel a running or queued encoding job.

        POST /api/encoding/queue/<item_id>/cancel. Called from
        encoding_queue.js's cancelEncodingItem().
        """
        ok, err = cancel_encoding_item(item_id)
        if ok:
            with _encoding_cancel_lock:
                ev = _encoding_active_cancel_events.get(item_id)
            if ev:
                ev.set()
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": err}), 400
    @app.route("/api/encoding/queue/clear", methods=["POST"])
    def api_encoding_queue_clear():
        """Remove all completed items from the encoding queue.

        POST /api/encoding/queue/clear. Called from encoding_queue.js's
        clearEncodingQueue().
        """
        clear_encoding_completed()
        return jsonify({"ok": True})
    @app.route("/api/encoding/queue/<int:item_id>/move", methods=["POST"])
    def api_encoding_queue_move(item_id):
        """Move an encoding queue item up or down in the queue order.

        POST /api/encoding/queue/<item_id>/move. Called from
        encoding_queue.js's moveEncodingItem(id, direction).
        """
        data = request.get_json(force=True, silent=True) or {}
        direction = data.get("direction", "up")
        ok, err = move_encoding_queue_item(item_id, direction)
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": err}), 400
    @app.route("/api/encoding/timing", methods=["GET"])
    def api_encoding_timing_get():
        """Return the encoding queue timing settings (separate from mode settings above).

        GET /api/encoding/timing. Called from templates/encoding.html's
        loadEncTiming(). Mirrors /api/upscale/settings' "mode" field, but for
        deciding when H.264/H.265 transcoding runs (see
        web/encoding_worker.py / models/common/common.py's
        _get_ffmpeg_codec_opts_for_download()).
        """
        return jsonify({
            "ok": True,
            "settings": {
                "timing":           get_setting("encoding_timing", "during_download"),
                "replace_original": get_setting("encoding_replace_original", "1"),
            }
        })
    @app.route("/api/encoding/timing", methods=["POST"])
    def api_encoding_timing_post():
        """Persist the encoding queue timing settings.

        POST /api/encoding/timing. Called from templates/encoding.html's
        saveEncTiming(). Kept as its own endpoint (rather than folding into
        /api/encoding/settings) so it can be saved independently of the
        active codec mode.
        """
        data = request.get_json(force=True) or {}
        timing = data.get("timing", "during_download")
        if timing not in ("during_download", "after_download"):
            timing = "during_download"
        set_setting("encoding_timing", timing)
        set_setting("encoding_replace_original", "1" if data.get("replace_original", True) else "0")
        return jsonify({"ok": True})
    @app.route("/api/encoding/detect-hw", methods=["POST"])
    def api_encoding_detect_hw():
        import subprocess
        import sys
        import time as _t

        global _detect_hw_cache, _detect_hw_cache_at

        # Return cached result if still fresh — avoids repeated 12s probe runs
        with _detect_hw_lock:
            if _detect_hw_cache is not None and (_t.time() - _detect_hw_cache_at) < _detect_hw_cache_ttl:
                return jsonify({"ok": True, "encoders": _detect_hw_cache, "cached": True})

        vaapi_device = get_setting("encoding_vaapi_device", "") or "/dev/dri/renderD128"

        def _reason_from_output(output: str, encoder: str) -> str:
            """Parse FFmpeg stderr and return a human-readable reason why an encoder failed."""
            o = output.lower()
            if encoder in ("h264_nvenc", "hevc_nvenc"):
                if "cannot load libcuda" in o or "libcuda.so" in o:
                    return "NVIDIA-Treiber nicht gefunden. Bei Docker: Container mit --gpus all starten."
                if "no nvenc capable devices" in o:
                    return "Keine NVIDIA-GPU gefunden oder NVENC nicht unterstützt."
                if "driver does not support" in o:
                    return "NVIDIA-Treiber zu alt — bitte aktualisieren."
                if "device or resource busy" in o:
                    return "GPU ist vollständig ausgelastet."
            if encoder in ("h264_vaapi", "hevc_vaapi"):
                if "no such file or directory" in o or "cannot open" in o:
                    return f"VAAPI-Gerät nicht gefunden ({vaapi_device}). Bei Docker: /dev/dri mounten."
                if "permission denied" in o:
                    return f"Kein Zugriff auf {vaapi_device}. Benutzer zur video-Gruppe hinzufügen."
                if "no va display" in o or "failed to initialise" in o:
                    return "VAAPI konnte nicht initialisiert werden — Treiber prüfen."
            if encoder in ("h264_videotoolbox", "hevc_videotoolbox"):
                if sys.platform != "darwin":
                    return "Nur auf macOS verfügbar."
            if "unknown encoder" in o or "encoder not found" in o or "no such encoder" in o:
                return "FFmpeg wurde ohne Support für diesen Encoder kompiliert."
            return "Encoder nicht verfügbar."

        def _probe(encoder: str, cmd: list, results: dict):
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=10
                )
                output = r.stdout + r.stderr
                if r.returncode == 0:
                    results[encoder] = {"available": True, "reason": None}
                else:
                    results[encoder] = {
                        "available": False,
                        "reason": _reason_from_output(output, encoder),
                    }
            except FileNotFoundError:
                results[encoder] = {"available": False, "reason": "ffmpeg nicht gefunden."}
            except subprocess.TimeoutExpired:
                results[encoder] = {"available": False, "reason": "Timeout — Encoder reagiert nicht."}
            except Exception as exc:
                results[encoder] = {"available": False, "reason": str(exc)}

        _null = ["ffmpeg", "-f", "lavfi", "-i", "nullsrc=size=256x256:rate=1",
                 "-frames:v", "1", "-an"]

        probes = {
            "libx264":            _null + ["-c:v", "libx264",            "-f", "null", "-"],
            "libx265":            _null + ["-c:v", "libx265",            "-f", "null", "-"],
            "h264_nvenc":         _null + ["-c:v", "h264_nvenc",         "-f", "null", "-"],
            "hevc_nvenc":         _null + ["-c:v", "hevc_nvenc",         "-f", "null", "-"],
            "h264_vaapi":         ["ffmpeg", "-vaapi_device", vaapi_device,
                                   "-f", "lavfi", "-i", "nullsrc=size=256x256:rate=1",
                                   "-frames:v", "1", "-an",
                                   "-vf", "format=nv12,hwupload",
                                   "-c:v", "h264_vaapi", "-f", "null", "-"],
            "hevc_vaapi":         ["ffmpeg", "-vaapi_device", vaapi_device,
                                   "-f", "lavfi", "-i", "nullsrc=size=256x256:rate=1",
                                   "-frames:v", "1", "-an",
                                   "-vf", "format=nv12,hwupload",
                                   "-c:v", "hevc_vaapi", "-f", "null", "-"],
            "h264_videotoolbox":  _null + ["-c:v", "h264_videotoolbox",  "-f", "null", "-"],
            "hevc_videotoolbox":  _null + ["-c:v", "hevc_videotoolbox",  "-f", "null", "-"],
        }

        results = {}
        threads = []
        for enc, cmd in probes.items():
            t = threading.Thread(target=_probe, args=(enc, cmd, results))
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=12)

        # Cache results so the next request is instant
        import time as _t2
        with _detect_hw_lock:
            _detect_hw_cache = results
            _detect_hw_cache_at = _t2.time()

        return jsonify({"ok": True, "encoders": results, "cached": False})
