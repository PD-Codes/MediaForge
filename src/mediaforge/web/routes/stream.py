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
import re
import threading
import time
from ...logger import get_logger


logger = get_logger(__name__)


def _stream_cors_origin():
    """Return the allowed CORS origin for HLS stream responses.
    Reflects the request Origin only when it matches the app host,
    so the streams are not accessible cross-origin if the token leaks."""
    req_origin = request.headers.get("Origin", "")
    app_origin = request.host_url.rstrip("/")
    return req_origin if req_origin == app_origin else app_origin




# ── Shared helpers ─────────────────────────────────────────────────────────

# Human names for the language codes ffprobe reports. Only the two UI
# languages are modelled; anything else falls back to the raw tag, which is
# still more useful than "Track 2".
_LANG_NAMES = {
    "de": ("Deutsch", "German"),      "ger": ("Deutsch", "German"),
    "deu": ("Deutsch", "German"),
    "en": ("Englisch", "English"),    "eng": ("Englisch", "English"),
    "ja": ("Japanisch", "Japanese"),  "jpn": ("Japanisch", "Japanese"),
    "fr": ("Französisch", "French"),  "fre": ("Französisch", "French"),
    "fra": ("Französisch", "French"),
    "es": ("Spanisch", "Spanish"),    "spa": ("Spanisch", "Spanish"),
    "it": ("Italienisch", "Italian"), "ita": ("Italienisch", "Italian"),
    "nl": ("Niederländisch", "Dutch"), "dut": ("Niederländisch", "Dutch"),
    "ru": ("Russisch", "Russian"),    "rus": ("Russisch", "Russian"),
    "ko": ("Koreanisch", "Korean"),   "kor": ("Koreanisch", "Korean"),
    "zh": ("Chinesisch", "Chinese"),  "chi": ("Chinesisch", "Chinese"),
    "tr": ("Türkisch", "Turkish"),    "tur": ("Türkisch", "Turkish"),
    "pl": ("Polnisch", "Polish"),     "pol": ("Polnisch", "Polish"),
}

_CHANNEL_NAMES = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}


def _ui_lang() -> str:
    from flask import session as _session
    try:
        return _session.get("ui_language", "de")
    except Exception:
        return "de"


def _lang_name(code: str, ui: str) -> str:
    pair = _LANG_NAMES.get((code or "").lower())
    if not pair:
        return (code or "").upper()
    return pair[0] if ui == "de" else pair[1]


def _track_label(track: dict, kind: str, ui: str) -> str:
    """Build the label the player shows for one audio/subtitle track.

    An embedded title wins when there is one -- a release group that wrote
    "Signs & Songs" knows better than we do what the track is for.
    """
    bits = []
    name = _lang_name(track.get("language", ""), ui)
    if name:
        bits.append(name)
    title = (track.get("title") or "").strip()
    if title and title.lower() not in (n.lower() for n in bits):
        bits.append(title)
    if kind == "audio":
        ch = _CHANNEL_NAMES.get(int(track.get("channels") or 0))
        if ch:
            bits.append(ch)
    if kind == "subtitle":
        if track.get("forced"):
            bits.append("Forced")
        if track.get("burn"):
            bits.append("Bitmap")
    if not bits:
        bits.append(("Tonspur" if ui == "de" else "Audio") if kind == "audio"
                    else ("Untertitel" if ui == "de" else "Subtitle"))
    return " · ".join(bits)


def _media_tracks_payload(info: dict) -> dict:
    """Turn a probe_file() result into the track/quality lists the player draws."""
    ui = _ui_lang()
    audio = [dict(t, label=_track_label(t, "audio", ui))
             for t in (info.get("audio_tracks") or [])]
    subs  = [dict(t, label=_track_label(t, "subtitle", ui))
             for t in (info.get("subtitle_tracks") or [])]

    from ..transcoder import QUALITY_LADDER
    src_h = int(info.get("height") or 0)
    qualities = [{"id": "auto",
                  "label": ("Original" if ui == "de" else "Original"),
                  "note": (f"{src_h}p" if src_h else ""),
                  "height": 0}]
    for h in QUALITY_LADDER:
        # Offering an upscale would cost CPU and gain nothing.
        if src_h and h >= src_h:
            continue
        qualities.append({"id": str(h), "label": f"{h}p", "height": h})

    return {
        "audio_tracks":    audio,
        "subtitle_tracks": subs,
        "qualities":       qualities if len(qualities) > 1 else [],
        "chapters":        info.get("chapters") or [],
    }


def _quality_height(value) -> int:
    """Map the player's quality id ('auto' / '720') to a scale height."""
    try:
        h = int(str(value or "auto"))
    except (TypeError, ValueError):
        return 0
    return h if h in (1080, 720, 480, 360) else 0


def _age_permits_path(resolved_path: str) -> bool:
    """Whether the current session may play the file at *resolved_path*.

    The rating is looked up for the TITLE the file belongs to, which here is
    the download history's record of where the file came from -- that is the
    one thing that maps a path back to a title without re-walking the library.
    A file the history does not know is treated as unrated and allowed, the
    same rule the rest of the age gate follows: an unrated file that is really
    a cartoon and a hard refusal are both wrong, and only one of them makes
    people switch the protection off.
    """
    from ..age_gate import ceiling, permits
    limit = ceiling()
    if limit is None:
        return True
    try:
        from ..db import get_download_history_meta_for_path, get_tmdb_cache
        from ..db import get_setting as _get_setting
        from flask import session as _session
        meta = get_download_history_meta_for_path(resolved_path) or {}
        title = str(meta.get("title") or "").strip()
        if not title:
            return True
        key = "%s|||%s|||%s" % (title,
                                _get_setting("cineinfo_country", "DE") or "DE",
                                _session.get("ui_language", "de"))
        return permits(get_tmdb_cache(key) or {}, limit)
    except Exception:
        logger.debug("[AgeGate] playback rating lookup failed", exc_info=True)
        return True


def _resolve_media_path(file_path: str):
    """Resolve a client-supplied path and confirm it is inside a library root.

    Every endpoint that touches a file by path has to go through this --
    the subtitle and thumbnail routes added for the player would otherwise
    be a plain arbitrary-file-read.

    It also applies the caller's library scope (web/groups.py): a group can
    restrict its members to some of the configured locations, and "cannot see
    it in the library but can still stream it by path" is not a restriction.
    Each root is checked against the same location id the library uses --
    "default" for the download root, the custom path id otherwise.
    """
    from pathlib import Path as _Path
    from ..db import get_custom_paths as _get_custom_paths

    if not file_path:
        return None
    try:
        resolved = _Path(file_path).expanduser().resolve()
    except Exception:
        return None

    raw_dl = get_setting("download_path") or os.environ.get("MEDIAFORGE_DOWNLOAD_PATH", "")
    roots = []          # (location_id, resolved root)
    if raw_dl:
        try:
            roots.append(("default", _Path(raw_dl).expanduser().resolve()))
        except Exception:
            pass
    else:
        roots.append(("default", (_Path.home() / "Downloads").resolve()))
    for cp in _get_custom_paths():
        try:
            roots.append((str(cp["id"]), _Path(cp["path"]).expanduser().resolve()))
        except Exception:
            pass

    try:
        from .library import lib_current_scope
        from ..groups import scope_allows
        scope = lib_current_scope()
    except Exception:
        scope, scope_allows = ["*"], None

    for location_id, root in roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if scope_allows is not None and not scope_allows(scope, location_id):
            # Inside the library but not inside this caller's part of it.
            # Keep looking rather than returning None: locations can nest.
            continue
        return resolved if resolved.is_file() else None
    return None


# Source probing: measured reachability for the Direct Play picker. Results
# are cached briefly and the number of concurrent outbound probes is capped,
# because opening the picker asks about every hoster at once.
_probe_cache: dict = {}
_probe_cache_lock = threading.Lock()
_probe_slots = threading.Semaphore(4)
_PROBE_TTL = 180.0



def _height_from_playlist(data: bytes) -> int:
    """Read the best RESOLUTION out of a master playlist, if this is one."""
    try:
        if not data or data[:7] != b"#EXTM3U":
            return 0
        text = data.decode("utf-8", "replace")
    except Exception:
        return 0
    best = 0
    for m in re.finditer(r"RESOLUTION=(\d+)x(\d+)", text):
        best = max(best, int(m.group(2)))
    return best


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
        audio_idx   = max(0, int(data.get("audio_index", 0) or 0))
        height      = _quality_height(data.get("quality"))

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
            # The codec of the SELECTED track, not of stream 0 -- copying an
            # AC3 track into mpegts because stream 0 happened to be AAC is
            # silence in the browser. Same fix as in /api/stream/start.
            _sel = next((t for t in (info.get("audio_tracks") or [])
                         if t.get("index") == audio_idx), None)
            acodec = ((_sel or {}).get("codec") or info.get("audio_codec") or "").lower()
            copy_video = vcodec in ("h264", "avc1")
            copy_audio = acodec in ("aac", "mp4a")
        except Exception as exc:
            logger.debug("[StreamSource] probe failed: %s", exc)

        actual_start = max(0.0, start_pos - 5.0)
        try:
            token, session = start_session(
                stream_url, actual_start, headers=headers,
                copy_video=copy_video, copy_audio=copy_audio,
                audio_index=audio_idx, height=height,
            )
        except RuntimeError as exc:
            err_str = str(exc)
            status_code = 429 if "Transcode-Sessions" in err_str else 503
            return jsonify({"error": err_str}), status_code

        payload = {
            "token":       token,
            "encoder":     "copy" if session.copy_video else session.encoder,
            "start_pos":   actual_start,
            "duration":    info.get("duration", 0),
            "video_codec": info.get("video_codec"),
            "audio_codec": info.get("audio_codec"),
            "height":      info.get("height", 0),
            "source":      True,
        }
        tracks = _media_tracks_payload(info)
        # Subtitles embedded in a REMOTE stream are dropped on purpose: the
        # extraction route only accepts library paths (it must, or it would
        # be an arbitrary-URL reader), so advertising them would give the
        # player entries it cannot load.
        tracks["subtitle_tracks"] = []
        payload.update(tracks)
        return jsonify(payload)
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
        # to the transcoder otherwise (e.g. a direct .mp4). Not every manifest
        # advertises itself by extension -- hanime's player fetches an
        # extension-less "/hls/<id>/<token>" path -- so the path counts too.
        _lower = stream_url.lower()
        is_hls = ".m3u8" in _lower or "/hls/" in _lower
        if not is_safe_url(stream_url):
            return jsonify({"error": "Unsichere Stream-URL", "hls": is_hls}), 400

        # Headers the episode itself resolved together with the URL win: a
        # signed, session-bound stream (hanime, behind Cloudflare Turnstile)
        # answers 403 to anything that doesn't replay its cookies and the
        # matching User-Agent. Everything else keeps the per-provider defaults.
        headers = {}
        try:
            headers = dict(getattr(episode, "stream_headers", None) or {})
        except Exception:
            headers = {}
        if not headers:
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
        """Start a transcode session.

        Body: {path, start_pos?, audio_index?, quality?, burn_subtitle?}
        """
        from ..transcoder import probe_file

        data       = request.get_json(force=True, silent=True) or {}
        file_path  = data.get("path", "")
        start_pos  = float(data.get("start_pos", 0) or 0)
        audio_idx  = max(0, int(data.get("audio_index", 0) or 0))
        height     = _quality_height(data.get("quality"))
        # NOT `... or -1`: track 0 is a perfectly normal subtitle index
        # (the usual position for PGS on a disc rip) and would be
        # swallowed as falsy, so burning it in silently did nothing.
        _burn_raw  = data.get("burn_subtitle", -1)
        burn_sub   = int(-1 if _burn_raw is None else _burn_raw)

        if not file_path:
            return jsonify({"error": "Datei nicht gefunden"}), 404

        resolved = _resolve_media_path(file_path)
        if resolved is None:
            return jsonify({"error": "Datei nicht gefunden"}), 404

        # The playback gate. Unlike the library listing (which filters what is
        # OFFERED), this refuses the file itself, so a path that was copied,
        # bookmarked, or guessed does not play either.
        if not _age_permits_path(str(resolved)):
            return jsonify({"error": "not permitted", "code": "age_limited"}), 403

        # Probe first so we can return media info
        info = probe_file(str(resolved)) or {}

        # Stream-copy when the local file is already browser-compatible H.264/AAC
        # (same reasoning as /api/stream/start-source): avoids an unnecessary
        # re-encode, which is both wasted CPU and the one place a hardware
        # encoder (VAAPI/NVENC) could reset a non-square SAR and change the
        # displayed aspect ratio even though width/height stay the same.
        vcodec = (info.get("video_codec") or "").lower()
        # The codec of the track that will actually be mapped -- info's
        # "audio_codec" is only the first stream, so copying it blindly
        # would hand the browser a DTS track the moment the viewer picks a
        # second language.
        _atracks = info.get("audio_tracks") or []
        _sel = next((t for t in _atracks if t.get("index") == audio_idx), None)
        acodec = ((_sel or {}).get("codec") or info.get("audio_codec") or "").lower()
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
                audio_index=audio_idx, height=height, burn_sub=burn_sub,
            )
        except RuntimeError as exc:
            err_str = str(exc)
            status_code = 429 if "Transcode-Sessions" in err_str else 503
            return jsonify({"error": err_str}), status_code

        payload = {
            "token":      token,
            "encoder":    "copy" if session.copy_video else session.encoder,
            "start_pos":  session.start_pos,
            "duration":   info.get("duration", 0),
            "video_codec": info.get("video_codec"),
            "audio_codec": info.get("audio_codec"),
            "width":      info.get("width", 0),
            "height":     info.get("height", 0),
            "format":     info.get("format", ""),
        }
        payload.update(_media_tracks_payload(info))
        return jsonify(payload)
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
    @app.route("/api/stream/subtitle")
    def api_stream_subtitle():
        """Serve one embedded text subtitle track as WebVTT.

        Query: ?path=<library file>&track=<index within the subtitle streams>
        The cues stay in FILE time; the player offsets them against the
        current transcode start itself, so a seek that restarts ffmpeg does
        not need a new extraction.
        """
        from ..transcoder import extract_subtitle_vtt, probe_file
        from flask import send_file

        resolved = _resolve_media_path(request.args.get("path", ""))
        if resolved is None:
            return jsonify({"error": "Datei nicht gefunden"}), 404
        try:
            track = int(request.args.get("track", "0"))
        except (TypeError, ValueError):
            return jsonify({"error": "Ungültiger Track"}), 400
        if track < 0 or track > 63:
            return jsonify({"error": "Ungültiger Track"}), 400

        # Refuse bitmap tracks here rather than handing back an empty file:
        # they have to be burned in by the transcoder instead.
        info = probe_file(str(resolved), timeout=15) or {}
        subs = info.get("subtitle_tracks") or []
        match = next((t for t in subs if t.get("index") == track), None)
        if match is None:
            return jsonify({"error": "Untertitelspur nicht gefunden"}), 404
        if match.get("burn"):
            return jsonify({"error": "Bildbasierte Untertitel müssen eingebrannt werden",
                            "burn": True}), 409

        vtt = extract_subtitle_vtt(str(resolved), track)
        if not vtt:
            return jsonify({"error": "Untertitel konnten nicht gelesen werden"}), 500
        resp = send_file(vtt, mimetype="text/vtt")
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    @app.route("/api/stream/thumbs")
    def api_stream_thumbs():
        """Report (and kick off) the seek-preview sprites for a library file."""
        from ..transcoder import thumbs_status, probe_file
        from flask import url_for

        resolved = _resolve_media_path(request.args.get("path", ""))
        if resolved is None:
            return jsonify({"error": "Datei nicht gefunden"}), 404

        info = probe_file(str(resolved), timeout=15) or {}
        duration = float(info.get("duration") or 0)
        # A frame every 10 s over a 6 h recording is a pointless amount of
        # decoding for a hover preview.
        if duration > 6 * 3600:
            return jsonify({"ready": False})

        st = thumbs_status(str(resolved), duration)
        if st.get("ready"):
            st["url"] = url_for("api_stream_thumb_sheet", key=st["key"], sheet=0) \
                .replace("/0.jpg", "/{n}.jpg")
        return jsonify(st)

    # Deliberately NOT under /api/stream/: the segment route below is
    # "/api/stream/<token>/<path:segment>", which would compete with a
    # four-part sprite URL for the same request.
    @app.route("/api/player/thumbs/<key>/<int:sheet>.jpg")
    def api_stream_thumb_sheet(key, sheet):
        """Serve one sprite sheet. The key is a cache hash, never a path."""
        from ..transcoder import thumb_sheet_path
        from flask import send_file
        p = thumb_sheet_path(key, sheet)
        if not p:
            return "Not found", 404
        resp = send_file(p, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    @app.route("/api/stream/probe-source", methods=["POST"])
    def api_stream_probe_source():
        """Measure one Direct Play source: is it reachable, and how fast?

        Body: {episode_url, provider, language}
        Answers {ok, ms, height?}. Results are cached for a few minutes and
        the number of concurrent probes is capped -- opening the source
        picker asks about every hoster at once, and each question is an
        outbound request.
        """
        from ..stream_proxy import fetch, is_safe_url

        data        = request.get_json(force=True, silent=True) or {}
        episode_url = (data.get("episode_url") or "").strip()
        provider    = (data.get("provider") or "").strip()
        language    = (data.get("language") or "").strip()
        if not episode_url or not provider:
            return jsonify({"ok": False, "error": "episode_url/provider fehlt"}), 400

        cache_key = (episode_url, provider, language)
        now = time.time()
        with _probe_cache_lock:
            hit = _probe_cache.get(cache_key)
            if hit and now - hit["at"] < _PROBE_TTL:
                return jsonify(hit["result"])

        if not _probe_slots.acquire(blocking=False):
            # Busy is not the same as broken: let the client ask again.
            return jsonify({"ok": False, "busy": True}), 202

        started = time.time()
        result = {"ok": False, "ms": 0}
        try:
            prov = resolve_provider(episode_url)
            episode = prov.episode_cls(
                url=episode_url, selected_language=language, selected_provider=provider,
            )
            stream_url = episode.stream_url
            if not stream_url or not is_safe_url(stream_url):
                raise ValueError("no usable stream url")

            headers = {}
            try:
                headers = dict(getattr(episode, "stream_headers", None) or {})
            except Exception:
                headers = {}
            if not headers:
                try:
                    from ...config import PROVIDER_HEADERS_D
                    headers = dict(PROVIDER_HEADERS_D.get(provider, {}) or {})
                except Exception:
                    headers = {}
            if not headers:
                headers = {"User-Agent": os.environ.get("MEDIAFORGE_USER_AGENT", "Mozilla/5.0")}

            # A two-byte range is enough to learn whether the hoster answers
            # at all; a playlist simply comes back whole and is still tiny.
            code, up_headers, body, _final = fetch(stream_url, headers, "bytes=0-1")
            ok = code in (200, 206)
            result = {"ok": ok, "ms": int((time.time() - started) * 1000)}
            if ok:
                height = _height_from_playlist(body)
                if height:
                    result["height"] = height
        except Exception as exc:
            logger.debug("[StreamProbe] %s/%s failed: %s", provider, language, exc)
            result = {"ok": False, "ms": int((time.time() - started) * 1000)}
        finally:
            _probe_slots.release()

        with _probe_cache_lock:
            if len(_probe_cache) > 400:
                _probe_cache.clear()
            _probe_cache[cache_key] = {"at": time.time(), "result": result}
        return jsonify(result)

    @app.route("/api/stream/markers")
    def api_stream_markers():
        """Intro/outro markers for an episode (AniWorld's aniskip data).

        Deliberately its own request: resolving them talks to an external
        API, and doing that inside /start would add its latency to every
        play click for a button that only appears a minute in.
        """
        episode_url = (request.args.get("url") or "").strip()
        if not episode_url:
            return jsonify({"markers": []})
        try:
            prov = resolve_provider(episode_url)
            episode = prov.episode_cls(url=episode_url)
            raw = getattr(episode, "skip_times", None) or {}
        except Exception as exc:
            logger.debug("[StreamMarkers] %s: %s", episode_url, exc)
            return jsonify({"markers": []})

        out = []
        for item in (raw.get("results") or []) if isinstance(raw, dict) else []:
            interval = item.get("interval") or {}
            try:
                start = float(interval.get("start_time") or 0)
                end   = float(interval.get("end_time") or 0)
            except (TypeError, ValueError):
                continue
            if end <= start:
                continue
            out.append({
                "start": start, "end": end,
                "kind": "outro" if item.get("skip_type") == "ed" else "intro",
            })
        return jsonify({"markers": out})

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
