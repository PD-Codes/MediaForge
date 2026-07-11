"""SyncPlay page/API routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

# TODO(telemetry): wire up flag.syncplay / detail.syncplay (session count,
# participant-count bucket, no room content) and syncplay.room_content
# (stage 5 -- which title is playing in a room) -- see registry.py. Not
# wired in this pass; only the registry entries exist so far.
"""

from ..db import get_setting
from ..db import set_setting
from flask import jsonify
from flask import render_template
from flask import request
from ..request_context import get_current_user_info as _get_current_user_info
from .. import runtime_state


_SYNCPLAY_STREAM_OK = {
    "api_stream_check", "api_stream_start", "api_stream_playlist",
    "api_stream_segment", "api_stream_status", "api_stream_stop",
    "api_stream_active",
}


def _syncplay_enabled() -> bool:
    """Return True if the SyncPlay feature is turned on in Settings."""
    return get_setting("syncplay_enabled", "0") == "1"


def _syncplay_device() -> str:
    """Classify the requesting client's device type from its User-Agent
    (Phone/Tablet/PC), used to label members in a SyncPlay room."""
    ua = (request.headers.get("User-Agent") or "").lower()
    if any(x in ua for x in ("iphone", "android", "mobile")):
        return "Phone"
    if any(x in ua for x in ("ipad", "tablet")):
        return "Tablet"
    return "PC"


def _sp_persist():
    """Save the current list of open SyncPlay room names to the settings DB
    so they can be reported/restored across restarts."""
    try:
        import json as _json
        from .. import syncplay_rooms as _sp
        set_setting("syncplay_rooms", _json.dumps(_sp.all_room_names()))
    except Exception:
        pass


def _sp_tok(data):
    """Extract and trim the ``token`` field from a parsed JSON request body."""
    return (data.get("token") or "").strip()


def register_syncplay_routes(app):
    """Register all SyncPlay page and API routes (watch-together rooms: join/
    leave, playback control relay, chat, host management) on the Flask app."""
    @app.route("/syncplay")
    def syncplay_page():
        """Serve GET /syncplay: the dedicated SyncPlay page. Guests reach this
        via an invite link; it is the only view they can see (the rest stays
        behind login)."""
        from ..db import get_setting as _gs
        if _gs("syncplay_enabled", "0") != "1":
            from flask import redirect, url_for
            return redirect(url_for("index"))
        room = (request.args.get("room") or "").strip()
        return render_template("syncplay.html", invite_room=room)
    @app.before_request
    def _syncplay_guest_stream_guard():
        """Gate the stream endpoints listed in _SYNCPLAY_STREAM_OK for
        unauthenticated requests: allow through only if the request carries a
        valid SyncPlay guest token, otherwise return 401. Runs before every
        request but is a no-op for endpoints outside that set."""
        if not runtime_state.AUTH_ENABLED:
            return None
        if request.endpoint not in _SYNCPLAY_STREAM_OK:
            return None
        from flask import session as _sess
        if _sess.get("user_id") is not None:
            return None  # logged-in user
        from .. import syncplay_rooms as sp
        if _syncplay_enabled() and sp.valid_token(_sess.get("sp_guest", "")):
            return None  # valid SyncPlay guest
        return jsonify({"error": "authentication required"}), 401
    @app.route("/api/syncplay/config", methods=["GET"])
    def api_syncplay_config():
        """Serve GET /api/syncplay/config: whether SyncPlay is enabled + the
        logged-in name to prefill the lobby. Called from static/syncplay_page.js
        during page init (the top-level `fetch('/api/syncplay/config')` call)."""
        user, _ = _get_current_user_info()
        return jsonify({
            "enabled": _syncplay_enabled(),
            "username": user or "",
            "can_manage": bool(user) or not runtime_state.AUTH_ENABLED,
        })
    @app.route("/api/syncplay/join", methods=["POST"])
    def api_syncplay_join():
        """Serve POST /api/syncplay/join: create or join a room by name,
        assigning a session token for the caller (host if the room is new).
        Called from static/syncplay_page.js's `S.join()`."""
        from .. import syncplay_rooms as sp
        if not _syncplay_enabled():
            return jsonify({"error": "SyncPlay ist deaktiviert"}), 403
        data = request.get_json(silent=True) or {}
        room = (data.get("room") or "").strip()
        if not room:
            return jsonify({"error": "room fehlt"}), 400
        # Logged-in users keep their name; guests pass one or get a Guest tag.
        user, _ = _get_current_user_info()
        is_guest = not user
        name = (data.get("username") or user or "").strip()
        try:
            token, _r, snap = sp.join(room, name, is_guest, _syncplay_device(),
                                      ip=request.remote_addr or "",
                                      password=(data.get("password") or None))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except sp.RoomError as exc:
            return jsonify({"error": str(exc)}), 403
        if is_guest:
            from flask import session as _sess
            _sess["sp_guest"] = token
        _sp_persist()
        return jsonify({"token": token, "snapshot": snap})
    @app.route("/api/syncplay/stream")
    def api_syncplay_stream():
        """Serve GET /api/syncplay/stream: Server-Sent Events stream of room
        events for one member. Opened from static/syncplay_page.js's
        `_openStream()` via `new EventSource(...)`."""
        from flask import Response, stream_with_context
        from .. import syncplay_rooms as sp
        import json as _json, queue as _queue
        token = (request.args.get("token") or "").strip()
        q = sp.subscribe(token)
        if q is None:
            return jsonify({"error": "invalid token"}), 404

        @stream_with_context
        def gen():
            yield "retry: 2000\n\n"
            while sp.valid_token(token):
                try:
                    ev = q.get(timeout=15)
                    sp.ack_drained(token, 1)
                    yield "data: " + _json.dumps(ev) + "\n\n"
                except _queue.Empty:
                    sp.heartbeat(token)
                    yield ": ping\n\n"
        resp = Response(gen(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp
    @app.route("/api/syncplay/control", methods=["POST"])
    def api_syncplay_control():
        """Serve POST /api/syncplay/control: relay a play/pause/seek action
        from the host to the rest of the room. Called from
        static/syncplay_page.js's `_ctrl()` (local play/pause/seek events) and
        `window._spOnUserSeek()` (explicit user seeks)."""
        from .. import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "").strip()
        if action not in ("play", "pause", "seek"):
            return jsonify({"error": "invalid action"}), 400
        pos = data.get("position")
        ok = sp.control((data.get("token") or "").strip(), action,
                        float(pos) if pos is not None else None)
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "session not found"}), 404))
    @app.route("/api/syncplay/report", methods=["POST"])
    def api_syncplay_report():
        """Serve POST /api/syncplay/report: a member reports their current
        playback position/paused/buffering state, used to keep the room's
        shared snapshot up to date. Called from static/syncplay_page.js's
        `_report()` (polled on an interval and on buffer/play events)."""
        from .. import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        ok = sp.report((data.get("token") or "").strip(),
                       float(data.get("position", 0) or 0),
                       bool(data.get("paused", True)),
                       bool(data.get("buffering", False)),
                       file=data.get("file"))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "session not found"}), 404))
    @app.route("/api/syncplay/ready", methods=["POST"])
    def api_syncplay_ready():
        """Serve POST /api/syncplay/ready: mark the calling member as
        ready/not-ready (e.g. finished buffering) so the room can wait for
        everyone before starting playback. No confirmed frontend caller was
        found in static/syncplay_page.js at the time of this audit."""
        from .. import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        ok = sp.set_ready((data.get("token") or "").strip(), bool(data.get("ready", True)))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "session not found"}), 404))
    @app.route("/api/syncplay/chat", methods=["POST"])
    def api_syncplay_chat():
        """Serve POST /api/syncplay/chat: post a chat message to the room.
        Called from static/syncplay_page.js's `S.sendChat()`."""
        from .. import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        ok = sp.chat((data.get("token") or "").strip(), str(data.get("text", "")))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "session not found"}), 404))
    @app.route("/api/syncplay/episode", methods=["POST"])
    def api_syncplay_episode():
        """Serve POST /api/syncplay/episode: host announces the currently
        selected media/episode (optionally starting a synced countdown before
        it plays). Called from static/syncplay_page.js's `_pick()` (episode
        picker) and `_onEnded()` (auto-advance to the next episode)."""
        from .. import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        token = (data.get("token") or "").strip()
        cd = data.get("countdown")
        if cd:
            ok = sp.start_countdown(token, data.get("media"), int(cd))
        else:
            ok = sp.set_media(token, data.get("media"))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host or no session"}), 403))
    @app.route("/api/syncplay/snapshot")
    def api_syncplay_snapshot():
        """Serve GET /api/syncplay/snapshot: resume an existing membership
        after a page reload. Called from static/syncplay_page.js's `_resync()`
        (event-queue overflow recovery) and `_tryResume()` (page load)."""
        from .. import syncplay_rooms as sp
        token = (request.args.get("token") or "").strip()
        snap = sp.get_snapshot(token)
        if snap is None:
            return jsonify({"error": "invalid"}), 404
        return jsonify({"token": token, "snapshot": snap})
    @app.route("/api/syncplay/leave", methods=["POST"])
    def api_syncplay_leave():
        """Serve POST /api/syncplay/leave: remove the caller from their room.
        Called from static/syncplay_page.js's `S.leave()` via `_beacon()`."""
        from .. import syncplay_rooms as sp
        from flask import session as _sess
        data = request.get_json(silent=True) or {}
        sp.leave((data.get("token") or "").strip())
        _sess.pop("sp_guest", None)
        return jsonify({"ok": True})
    @app.route("/api/syncplay/rooms", methods=["GET"])
    def api_syncplay_rooms():
        """Serve GET /api/syncplay/rooms: list open SyncPlay rooms for the
        lobby directory. Called from static/syncplay_page.js's `_loadRooms()`."""
        from .. import syncplay_rooms as sp
        if not _syncplay_enabled():
            return jsonify({"rooms": []})
        return jsonify({"rooms": sp.list_rooms()})
    @app.route("/api/syncplay/close-room", methods=["POST"])
    def api_syncplay_close_room():
        """Serve POST /api/syncplay/close-room: force-close a room by name
        from the lobby directory. Called from static/syncplay_page.js's
        `S.closeRoomByName()`."""
        # Owner-only: this endpoint stays behind login_required (not exempt),
        # so guests cannot close rooms — only the instance owner can.
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.close_by_name((d.get("name") or "").strip())
        _sp_persist()
        return jsonify({"ok": ok})
    @app.route("/api/syncplay/kick", methods=["POST"])
    def api_syncplay_kick():
        """Serve POST /api/syncplay/kick: host removes a member from the
        room. Called from static/syncplay_page.js's `S.kick()`."""
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.kick(_sp_tok(d), (d.get("name") or "").strip())
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))
    @app.route("/api/syncplay/ban", methods=["POST"])
    def api_syncplay_ban():
        """Serve POST /api/syncplay/ban: host bans a member (optionally by
        IP) from the room. Called from static/syncplay_page.js's `S.ban()`."""
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.ban(_sp_tok(d), (d.get("name") or "").strip(), bool(d.get("by_ip", True)))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))
    @app.route("/api/syncplay/transfer-host", methods=["POST"])
    def api_syncplay_transfer_host():
        """Serve POST /api/syncplay/transfer-host: current host hands host
        privileges to another member. Called from static/syncplay_page.js's
        `S.transferHost()`."""
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.transfer_host(_sp_tok(d), (d.get("name") or "").strip())
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))
    @app.route("/api/syncplay/close", methods=["POST"])
    def api_syncplay_close():
        """Serve POST /api/syncplay/close: host closes their own room for
        everyone. Called from static/syncplay_page.js's `S.closeRoom()`."""
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.close_room(_sp_tok(d))
        _sp_persist()
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))
    @app.route("/api/syncplay/host-lock", methods=["POST"])
    def api_syncplay_host_lock():
        """Serve POST /api/syncplay/host-lock: host toggles host-only
        playback control (members can't drive play/pause/seek while locked).
        Called from static/syncplay_page.js's `S.setHostLock()`."""
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.set_host_lock(_sp_tok(d), bool(d.get("locked", False)))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))
    @app.route("/api/syncplay/max", methods=["POST"])
    def api_syncplay_max():
        """Serve POST /api/syncplay/max: host sets the room's max member
        count. Called from static/syncplay_page.js's `S.setMax()`."""
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.set_max(_sp_tok(d), d.get("max"))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))
    @app.route("/api/syncplay/password", methods=["POST"])
    def api_syncplay_password():
        """Serve POST /api/syncplay/password: host sets/clears the room's
        join password. Called from static/syncplay_page.js's
        `S.setRoomPassword()`."""
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.set_password(_sp_tok(d), d.get("password"))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))
    @app.route("/api/syncplay/away", methods=["POST"])
    def api_syncplay_away():
        """Serve POST /api/syncplay/away: mark the caller as away/back
        (e.g. tab hidden). Called from static/syncplay_page.js's
        `_onVisibility()`."""
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        sp.set_away(_sp_tok(d), bool(d.get("away", False)))
        return jsonify({"ok": True})
    @app.route("/api/syncplay/typing", methods=["POST"])
    def api_syncplay_typing():
        """Serve POST /api/syncplay/typing: report the caller's chat-typing
        state. Called from static/syncplay_page.js's `_setTyping()`."""
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        sp.typing(_sp_tok(d), bool(d.get("typing", False)))
        return jsonify({"ok": True})
    @app.route("/api/syncplay/reaction", methods=["POST"])
    def api_syncplay_reaction():
        """Serve POST /api/syncplay/reaction: broadcast an emoji reaction to
        the room. Called from static/syncplay_page.js's `S.react()`."""
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        sp.reaction(_sp_tok(d), str(d.get("emoji", "")))
        return jsonify({"ok": True})
    @app.route("/api/syncplay/track", methods=["POST"])
    def api_syncplay_track():
        """Serve POST /api/syncplay/track: relay a track-related setting
        (e.g. playback rate) from the host to the room. Called from
        static/syncplay_page.js's `_onRate()`."""
        from .. import syncplay_rooms as sp
        d = request.get_json(silent=True) or {}
        ok = sp.set_track(_sp_tok(d), (d.get("kind") or "").strip(), d.get("value"))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "not host"}), 403))
