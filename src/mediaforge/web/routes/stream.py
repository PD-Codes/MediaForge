"""Streaming / proxy routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

# TODO(telemetry): wire up stream.play_events (stage 5 -- which title/
# episode was started, no watch time) and syncplay.room_content (stage 5,
# in routes/syncplay.py) -- see telemetry.events.build_play_event() and
# telemetry/registry.py. Registry-only for now.
"""

from ...providers import resolve_provider
from ..db import get_setting
from flask import jsonify
from flask import request
import os
from ...logger import get_logger


logger = get_logger(__name__)


def _stream_cors_origin():
    """Return the allowed CORS origin for HLS stream responses.
    Reflects the request Origin only when it matches the app host,
    so the streams are not accessible cross-origin if the token leaks."""
    req_origin = request.headers.get("Origin", "")
    app_origin = request.host_url.rstrip("/")
    return req_origin if req_origin == app_origin else app_origin


def register_stream_routes(app):
    """Register the transcode/HLS-proxy streaming routes on the given Flask app."""
    @app.route("/api/stream/check")
    def api_stream_check():
        """Return available encoder info (no ffmpeg process started)."""
        from ..transcoder import get_best_encoder, detect_available_encoders
        import shutil as _s
        if not _s.which("ffmpeg"):
            return jsonify({"available": False, "reason": "ffmpeg nicht gefunden"})
        all_enc = detect_available_encoders()
        encoder, is_hw = get_best_encoder()
        if not encoder:
            return jsonify({"available": False, "reason": "Kein kompatibler H.264-Encoder gefunden",
                            "all": all_enc})
        return jsonify({"available": True, "encoder": encoder, "is_hardware": is_hw,
                        "all": all_enc})
    @app.route("/api/stream/reset-encoders", methods=["POST"])
    def api_stream_reset_encoders():
        """Clear encoder cache — forces re-detection on next request."""
        from ..transcoder import reset_encoder_cache
        from .encoding import reset_detect_hw_cache
        reset_encoder_cache()
        reset_detect_hw_cache()
        return jsonify({"ok": True, "message": "Encoder-Cache geleert"})
    @app.route("/api/stream/start-source", methods=["POST"])
    def api_stream_start_source():
        """Stream an episode directly from its provider (no prior download).

        Body: {episode_url, provider?, language?, start_pos?}
        Resolves the provider's direct stream URL on demand and feeds it to the
        transcoder with the provider's HTTP headers.
        """
        from ..transcoder import start_session, probe_file

        data        = request.get_json(force=True, silent=True) or {}
        episode_url = (data.get("episode_url") or "").strip()
        provider    = (data.get("provider") or "VOE").strip()
        language    = (data.get("language") or "German Dub").strip()
        start_pos   = float(data.get("start_pos", 0) or 0)

        if not episode_url:
            return jsonify({"error": "episode_url fehlt"}), 400

        # ── Resolve the direct stream URL via the model/extractor layer ──
        try:
            prov = resolve_provider(episode_url)
            episode = prov.episode_cls(
                url=episode_url,
                selected_language=language,
                selected_provider=provider,
            )
            stream_url = episode.stream_url
        except Exception as exc:
            logger.warning("[StreamSource] resolve failed for %s (%s/%s): %s",
                           episode_url, provider, language, exc)
            return jsonify({"error": f"Stream konnte nicht aufgelöst werden: {exc}"}), 502

        if not stream_url:
            return jsonify({"error": "Kein Stream-Link gefunden"}), 502

        # Provider-specific HTTP headers (Referer / User-Agent) for ffmpeg.
        try:
            from ...config import PROVIDER_HEADERS_D
            headers = dict(PROVIDER_HEADERS_D.get(provider, {}) or {})
        except Exception:
            headers = {}
        # Ensure ffmpeg treats the input as a remote source even if the
        # provider has no special headers configured.
        if not headers:
            headers = {"User-Agent": os.environ.get("MEDIAFORGE_USER_AGENT", "Mozilla/5.0")}

        # Probe the resolved stream so we can stream-copy when the source is
        # already browser-compatible (H.264/AAC) — this avoids re-encoding,
        # which is the main cause of stutter on slower machines.
        # Stream-copy when the source is already browser-compatible (least bad
        # of the ffmpeg options). The real fix for the residual stutter is the
        # passthrough proxy below, which avoids ffmpeg entirely for HLS sources.
        info = {}
        copy_video = False
        copy_audio = False
        try:
            info = probe_file(stream_url, headers=headers) or {}
            vcodec = (info.get("video_codec") or "").lower()
            acodec = (info.get("audio_codec") or "").lower()
            copy_video = vcodec in ("h264", "avc1")
            copy_audio = acodec in ("aac", "mp4a")
        except Exception as exc:
            logger.debug("[StreamSource] probe failed: %s", exc)

        actual_start = max(0.0, start_pos - 5.0)
        try:
            token, session = start_session(
                stream_url, actual_start, headers=headers,
                copy_video=copy_video, copy_audio=copy_audio,
            )
        except RuntimeError as exc:
            err_str = str(exc)
            status_code = 429 if "Transcode-Sessions" in err_str else 503
            return jsonify({"error": err_str}), status_code

        return jsonify({
            "token":       token,
            "encoder":     "copy" if copy_video else session.encoder,
            "start_pos":   actual_start,
            "duration":    info.get("duration", 0),
            "video_codec": info.get("video_codec"),
            "audio_codec": info.get("audio_codec"),
            "source":      True,
        })
    @app.route("/api/stream/start-proxy", methods=["POST"])
    def api_stream_start_proxy():
        """Play an episode by proxying its native provider HLS (no ffmpeg).

        Resolves the provider's stream URL + headers, then returns a proxied
        playlist URL the browser can hand straight to hls.js. This avoids the
        transcoder entirely and is the smooth, low-CPU path for HLS sources.
        """
        from ..stream_proxy import create_proxy_session, b64e, is_safe_url

        data        = request.get_json(force=True, silent=True) or {}
        episode_url = (data.get("episode_url") or "").strip()
        provider    = (data.get("provider") or "VOE").strip()
        language    = (data.get("language") or "German Dub").strip()
        if not episode_url:
            return jsonify({"error": "episode_url fehlt"}), 400

        try:
            prov = resolve_provider(episode_url)
            episode = prov.episode_cls(
                url=episode_url, selected_language=language, selected_provider=provider,
            )
            stream_url = episode.stream_url
        except Exception as exc:
            logger.warning("[StreamProxy] resolve failed for %s (%s/%s): %s",
                           episode_url, provider, language, exc)
            return jsonify({"error": f"Stream konnte nicht aufgelöst werden: {exc}"}), 502

        if not stream_url:
            return jsonify({"error": "Kein Stream-Link gefunden"}), 502
        # Only HLS can be proxied as a playlist; signal the client to fall back
        # to the transcoder otherwise (e.g. a direct .mp4).
        is_hls = ".m3u8" in stream_url.lower()
        if not is_safe_url(stream_url):
            return jsonify({"error": "Unsichere Stream-URL", "hls": is_hls}), 400

        try:
            from ...config import PROVIDER_HEADERS_D
            headers = dict(PROVIDER_HEADERS_D.get(provider, {}) or {})
        except Exception:
            headers = {}
        if not headers:
            headers = {"User-Agent": os.environ.get("MEDIAFORGE_USER_AGENT", "Mozilla/5.0")}

        token = create_proxy_session(headers)
        playlist_url = f"/api/proxy/{token}/r/{b64e(stream_url)}"
        return jsonify({"token": token, "playlist_url": playlist_url, "hls": is_hls, "source": True})
    @app.route("/api/proxy/<token>/r/<path:b64>")
    def api_proxy_resource(token, b64):
        """Fetch + (for playlists) rewrite a provider resource through the proxy."""
        from flask import Response as _Response
        from ..stream_proxy import (get_proxy_session, b64d, fetch,
                                    is_playlist, rewrite_playlist, is_safe_url)
        sess = get_proxy_session(token)
        if not sess:
            return "Session not found", 404
        try:
            url = b64d(b64)
        except Exception:
            return "Bad resource", 400
        if not is_safe_url(url):
            return "Forbidden", 403
        try:
            code, up_headers, data, final_url = fetch(
                url, sess["headers"], request.headers.get("Range"))
        except Exception as exc:
            logger.debug("[StreamProxy] fetch failed: %s", exc)
            return jsonify({"error": "Upstream nicht erreichbar"}), 502

        if is_playlist(data):
            text = data.decode("utf-8", "replace")
            proxy_base = f"/api/proxy/{token}/r/"
            body = rewrite_playlist(text, final_url, proxy_base)
            resp = _Response(body, mimetype="application/vnd.apple.mpegurl")
        else:
            resp = _Response(data, status=code)
            for h in ("Content-Type", "Content-Range", "Accept-Ranges", "Content-Length"):
                if h in up_headers:
                    resp.headers[h] = up_headers[h]
            if "Content-Type" not in up_headers:
                resp.headers["Content-Type"] = "video/mp2t"
        resp.headers["Access-Control-Allow-Origin"] = _stream_cors_origin()
        resp.headers["Vary"] = "Origin"
        resp.headers["Cache-Control"] = "no-cache"
        return resp
    @app.route("/api/stream/close-proxy", methods=["POST"])
    def api_stream_close_proxy():
        """Close an HLS proxy session. Body: {token}"""
        from ..stream_proxy import close_proxy_session
        data = request.get_json(silent=True) or {}
        tok = (data.get("token") or "").strip()
        if tok:
            close_proxy_session(tok)
        return jsonify({"ok": True})
    @app.route("/api/stream/start", methods=["POST"])
    def api_stream_start():
        """Start a transcode session. Body: {path, start_pos?}"""
        from ..transcoder import start_session, probe_file
        from pathlib import Path as _Path
        from ..db import get_custom_paths as _get_custom_paths

        data       = request.get_json(force=True, silent=True) or {}
        file_path  = data.get("path", "")
        start_pos  = float(data.get("start_pos", 0) or 0)

        if not file_path:
            return jsonify({"error": "Datei nicht gefunden"}), 404

        # Resolve path and validate against allowed library roots
        try:
            resolved = _Path(file_path).resolve()
        except Exception:
            return jsonify({"error": "Ungültiger Pfad"}), 400

        _raw_dl = get_setting("download_path") or os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
        _allowed_roots = []
        if _raw_dl:
            try:
                _allowed_roots.append(_Path(_raw_dl).expanduser().resolve())
            except Exception:
                pass
        else:
            _allowed_roots.append((_Path.home() / "Downloads").resolve())

        for _cp in _get_custom_paths():
            try:
                _allowed_roots.append(_Path(_cp["path"]).expanduser().resolve())
            except Exception:
                pass

        _path_ok = False
        for _root in _allowed_roots:
            try:
                resolved.relative_to(_root)
                _path_ok = True
                break
            except ValueError:
                pass

        if not _path_ok or not resolved.is_file():
            return jsonify({"error": "Datei nicht gefunden"}), 404

        # Probe first so we can return media info
        info = probe_file(str(resolved)) or {}

        # Stream-copy when the local file is already browser-compatible H.264/AAC
        # (same reasoning as /api/stream/start-source): avoids an unnecessary
        # re-encode, which is both wasted CPU and the one place a hardware
        # encoder (VAAPI/NVENC) could reset a non-square SAR and change the
        # displayed aspect ratio even though width/height stay the same.
        vcodec = (info.get("video_codec") or "").lower()
        acodec = (info.get("audio_codec") or "").lower()
        copy_video = vcodec in ("h264", "avc1")
        copy_audio = acodec in ("aac", "mp4a")

        # Start a bit before saved position for buffer
        actual_start = max(0.0, start_pos - 5.0)

        # SyncPlay: everyone in a room watches the same file at the same spot, so
        # share ONE transcode session (and its segments) instead of one ffmpeg
        # per viewer. The share key is derived from the room server-side.
        from ..transcoder import start_or_join_session
        share_key = None
        _sp_tok = (data.get("syncplay_token") or "").strip()
        if _sp_tok:
            try:
                from .. import syncplay_rooms as _sp
                _room = _sp.room_for_token(_sp_tok)
                if _room:
                    share_key = "sp:" + _room.name
            except Exception:
                share_key = None

        try:
            token, session = start_or_join_session(
                str(resolved), actual_start, share_key=share_key,
                copy_video=copy_video, copy_audio=copy_audio,
                display_aspect_ratio=info.get("display_aspect_ratio"),
            )
        except RuntimeError as exc:
            err_str = str(exc)
            status_code = 429 if "Transcode-Sessions" in err_str else 503
            return jsonify({"error": err_str}), status_code

        return jsonify({
            "token":      token,
            "encoder":    "copy" if copy_video else session.encoder,
            "start_pos":  session.start_pos,
            "duration":   info.get("duration", 0),
            "video_codec": info.get("video_codec"),
            "audio_codec": info.get("audio_codec"),
            "width":      info.get("width", 0),
            "height":     info.get("height", 0),
            "format":     info.get("format", ""),
        })
    @app.route("/api/stream/<token>/index.m3u8")
    def api_stream_playlist(token):
        """Serve the HLS master playlist for a session."""
        from ..transcoder import get_session
        import time as _t
        sess = get_session(token)
        if not sess:
            return "Session not found", 404

        # Wait for the background thread to signal playlist readiness
        sess._playlist_ready.wait(timeout=30)
        if not (sess.playlist_path and os.path.exists(sess.playlist_path)):
            err = sess.error or "Timeout: kein Segment innerhalb von 30 s"
            logger.warning("[Stream] playlist not ready for %s: %s", token[:8], err)
            return jsonify({"error": err}), 503
        # Verify at least one .ts reference is present
        try:
            with open(sess.playlist_path) as _pf:
                if ".ts" not in _pf.read():
                    err = sess.error or "Playlist ohne Segmente"
                    return jsonify({"error": err}), 503
        except Exception:
            return jsonify({"error": "Playlist nicht lesbar"}), 503

        from flask import send_file
        resp = send_file(sess.playlist_path, mimetype="application/vnd.apple.mpegurl")
        resp.headers["Cache-Control"] = "no-cache, no-store"
        resp.headers["Access-Control-Allow-Origin"] = _stream_cors_origin()
        resp.headers["Vary"] = "Origin"
        return resp
    @app.route("/api/stream/<token>/<path:segment>")
    def api_stream_segment(token, segment):
        """Serve a .ts segment for a session."""
        from ..transcoder import get_session
        from pathlib import Path as _Path
        import re as _re
        import time as _t

        sess = get_session(token)
        if not sess or not sess.tmp_dir:
            return "Session not found", 404

        # Accept only safe bare filenames — no path separators, no traversal
        bare = _Path(segment).name
        if not _re.fullmatch(r"seg\d+\.ts", bare):
            return "Segment not found", 404

        tmp_dir = _Path(sess.tmp_dir).resolve()
        seg_path = (tmp_dir / bare).resolve()

        # Ensure the resolved path is still inside the session tmp dir
        try:
            seg_path.relative_to(tmp_dir)
        except ValueError:
            return "Segment not found", 404

        # Wait up to 5 s for the segment to be written; return 503 so hls.js retries
        deadline = _t.time() + 5
        while _t.time() < deadline:
            if seg_path.exists() and seg_path.stat().st_size > 0:
                break
            _t.sleep(0.1)

        if not (seg_path.exists() and seg_path.stat().st_size > 0):
            from flask import Response as _Resp
            return _Resp("Segment not yet available", status=503,
                         headers={"Retry-After": "1"})

        from flask import send_file
        resp = send_file(str(seg_path), mimetype="video/mp2t")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Access-Control-Allow-Origin"] = _stream_cors_origin()
        resp.headers["Vary"] = "Origin"
        return resp
    @app.route("/api/stream/stop", methods=["POST"])
    def api_stream_stop():
        """Stop a transcode session. Body: {token}"""
        from ..transcoder import stop_session
        data  = request.get_json(force=True, silent=True) or {}
        token = data.get("token", "")
        if token:
            stop_session(token)
        return jsonify({"ok": True})
    @app.route("/api/stream/active")
    def api_stream_active():
        """Return active stream count (for sidebar badge)."""
        from ..transcoder import active_count
        return jsonify({"count": active_count()})
    @app.route("/api/stream/<token>/status")
    def api_stream_status(token):
        """Poll session readiness: {ready, error, alive, stderr_tail}"""
        from ..transcoder import get_session
        sess = get_session(token)
        if not sess:
            return jsonify({"ready": False, "error": "Session nicht gefunden", "alive": False})
        alive = sess.is_alive()
        # Check if playlist has segments
        ready = False
        if sess.playlist_path and os.path.exists(sess.playlist_path):
            try:
                with open(sess.playlist_path) as _pf:
                    ready = ".ts" in _pf.read()
            except Exception:
                pass
        # Try to read stderr tail (non-blocking peek)
        stderr_tail = ""
        if sess.process and sess.process.stderr:
            import select, os as _os
            try:
                # Non-blocking read on Windows via os.read with a small chunk
                fd = sess.process.stderr.fileno()
                # Drain up to 4 KB without blocking
                chunk = b""
                try:
                    import msvcrt
                    # Windows: check if data available
                    while msvcrt.kbhit() if False else True:
                        c = _os.read(fd, 4096)
                        if c:
                            chunk += c
                        break
                except Exception:
                    pass
                if chunk:
                    stderr_tail = chunk.decode(errors="replace")[-300:]
                    # Cache it on the session for death diagnosis
                    sess._stderr_buf = getattr(sess, "_stderr_buf", "") + stderr_tail
            except Exception:
                pass
        # If process died without segments, collect stderr
        if not alive and not ready:
            err = sess.error or "ffmpeg beendet ohne Ausgabe"
            if sess.process:
                try:
                    out = sess.process.stderr.read()
                    buf = getattr(sess, "_stderr_buf", "")
                    full = (buf + out.decode(errors="replace"))[-600:] if out else buf[-600:]
                    if full:
                        err = err + ": " + full
                        sess.error = err
                except Exception:
                    pass
            return jsonify({"ready": False, "error": err, "alive": False})
        return jsonify({"ready": ready, "error": sess.error, "alive": alive,
                        "stderr_tail": stderr_tail})
