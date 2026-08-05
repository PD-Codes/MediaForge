"""SyncPlay page/API routes.

Extracted from create_app as a plain route-registration function
(no Flask blueprint: endpoint names stay bare so url_for() keeps working).

Telemetry: flag.syncplay (stage-2 usage counter) is submitted once per
created/joined session by the join route below; detail.syncplay
("started"/"ended" plus a coarse participant bracket) lives in
web/syncplay_rooms.py, which owns the room lifecycle. Nothing is ever
reported from the SSE stream: it ticks every few seconds for every connected
member, and a torn-down stream is the user closing a tab, not a failure.

syncplay.room_content (stage 5, the title playing in a room) is reported from
web/syncplay_rooms.py too, once per actual media change announced by the host
-- via build_play_event(data_key="syncplay.room_content"), which checks that
key's own consent toggle rather than stream.play_events'.
"""

from ..db import get_setting
from flask import jsonify
from flask import render_template
from flask import request
from ..request_context import get_current_user_info as _get_current_user_info
from .. import runtime_state
from ...logger import get_logger
from ...telemetry import classify as telemetry_classify
from ...telemetry import client as telemetry_client
from ...telemetry import events as telemetry_events


logger = get_logger(__name__)


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
        from .. import syncplay_rooms as _sp
        from ..db import set_json_setting
        set_json_setting("syncplay_rooms", _sp.all_room_names())
    except Exception:
        pass


def _sp_tok(data):
    """Extract and trim the ``token`` field from a parsed JSON request body."""
    return (data.get("token") or "").strip()


def _report_syncplay_join():
    """Submit the flag.syncplay stage-2 usage counter for one created/joined
    session.

    A pure counter -- build_feature_flag_event() takes no metadata at all, so
    no room name, member name or watched title is involved. Fires once per
    successful join, never per SSE tick or per control/report call. The
    session lifecycle detail (detail.syncplay) is reported by
    syncplay_rooms.join() itself.

    Wrapped in its own try/except so a telemetry bug can never break joining.
    """
    try:
        telemetry_client.submit(telemetry_events.build_feature_flag_event("flag.syncplay"))
    except Exception:
        logger.debug("[Telemetry] failed to build/submit flag.syncplay event", exc_info=True)


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
        # ?invite=<code> is the revocable, expiring form of ?room=<name>.
        # Resolved here rather than in the client so the existing lobby needs
        # no new code: it already prefills from `invite_room`. A dead or
        # expired code simply lands on the normal lobby -- telling a visitor
        # that the code WAS valid once is information they have no use for and
        # that confirms a room exists.
        code = (request.args.get("invite") or "").strip()
        if code and not room:
            from .. import syncplay_rooms as sp
            room = sp.resolve_invite(code) or ""
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
        _guest = _sess.get("sp_guest", "")
        if _syncplay_enabled() and sp.valid_token(_guest):
            # A guest token is permission to watch what the room is watching --
            # not a library-wide read pass. api_stream_start takes the path
            # from the request body, so without this check a guest who joined
            # an open room could stream any file inside the library roots whose
            # path they knew or guessed.
            if request.endpoint == "api_stream_start":
                _wanted = ((request.get_json(silent=True) or {}).get("path") or "").strip()
                _room_file = sp.media_file_for_token(_guest)
                if not _room_file or not _wanted:
                    return jsonify({"error": "no media selected in this room"}), 403
                try:
                    from pathlib import Path as _P
                    if _P(_wanted).resolve() != _P(_room_file).resolve():
                        return jsonify({"error": "not the file this room is watching"}), 403
                except OSError:
                    return jsonify({"error": "invalid path"}), 400
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
        _report_syncplay_join()
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
            # Deliberately silent for telemetry: this loop wakes up every 15s
            # for every connected member, so anything reported from here would
            # count heartbeats instead of usage. The session is reported once
            # at join (flag.syncplay above, detail.syncplay in syncplay_rooms).
            yield "retry: 2000\n\n"
            try:
                while sp.valid_token(token):
                    try:
                        ev = q.get(timeout=15)
                        sp.ack_drained(token, 1)
                        yield "data: " + _json.dumps(ev) + "\n\n"
                    except _queue.Empty:
                        sp.heartbeat(token)
                        yield ": ping\n\n"
            except BaseException as exc:
                # A closed browser tab tears this generator down with
                # GeneratorExit / ConnectionResetError / BrokenPipeError. That
                # is the user leaving, never a defect: it must not be logged as
                # an error (the telemetry log handler turns logger.error() into
                # a crash report) and must not produce any error-flavoured
                # event. Only a genuinely unexpected exception is even worth a
                # debug line; either way the exception is re-raised unchanged so
                # Flask tears the response down exactly as before.
                #
                # is_client_disconnect(), NOT is_user_cancellation(): those three
                # exception types only mean "the client went away" while writing
                # a response, which is exactly where this code is. Everywhere
                # else they are ordinary network failures and must stay
                # reportable -- see telemetry/classify.py for why the two checks
                # are deliberately separate.
                if not telemetry_classify.is_client_disconnect(type(exc), exc):
                    logger.debug("[SyncPlay] SSE stream ended unexpectedly: %s", exc)
                raise
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
    @app.route("/api/syncplay/reaction", methods=["POST"])
    def api_syncplay_react():
        """Serve POST /api/syncplay/react: send a reaction into the room.

        Separate from chat on purpose: a reaction is a moment, not a record.
        It is broadcast and forgotten rather than appended to the chat
        history, so twenty people tapping the same emoji at a cliffhanger
        does not push the conversation off the top of the log.

        The emoji is a key from a closed set, not free text -- an arbitrary
        string rendered into every participant's player is not something to
        accept from a guest.
        """
        from .. import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        ok = sp.react((data.get("token") or "").strip(),
                      str(data.get("emoji", "")))
        if ok:
            return jsonify({"ok": True})
        # 429, not 404: the common reason to land here is the per-member rate
        # limit, and telling somebody their session is gone when they just
        # tapped too fast sends them to reload a working page.
        return jsonify({"error": "not allowed or too fast"}), 429

    @app.route("/api/syncplay/invite", methods=["POST"])
    def api_syncplay_invite():
        """Serve POST /api/syncplay/invite: the host creates a share link.

        An invite is a separate object from the room name: sharing the name is
        forever and cannot be taken back, while a link can be given a lifetime
        and revoked without renaming the room out from under everybody already
        in it.

        Host only — a guest handing out further invites is how a private room
        stops being one.
        """
        from .. import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        invite = sp.create_invite(
            (data.get("token") or "").strip(),
            minutes=int(data.get("minutes") or 60),
            uses=(int(data["uses"]) if str(data.get("uses") or "").isdigit() else None),
        )
        if not invite:
            return jsonify({"error": "not the host, or too many live invites"}), 403
        base = (get_setting("web_base_url", "") or "").rstrip("/")
        invite["url"] = "%s/syncplay?invite=%s" % (base, invite["code"])
        return jsonify(invite)

    @app.route("/api/syncplay/invites", methods=["GET"])
    def api_syncplay_invites():
        """Serve GET /api/syncplay/invites?token=…: the host's live invites."""
        from .. import syncplay_rooms as sp
        return jsonify({"invites": sp.list_invites(
            (request.args.get("token") or "").strip())})

    @app.route("/api/syncplay/invite/revoke", methods=["POST"])
    def api_syncplay_invite_revoke():
        """Serve POST /api/syncplay/invite/revoke: kill one share link."""
        from .. import syncplay_rooms as sp
        data = request.get_json(silent=True) or {}
        ok = sp.revoke_invite((data.get("token") or "").strip(),
                              str(data.get("code", "")))
        return (jsonify({"ok": True}) if ok else (jsonify({"error": "unknown"}), 404))

    @app.route("/api/syncplay/invite/resolve")
    def api_syncplay_invite_resolve():
        """Serve GET /api/syncplay/invite/resolve?code=…: which room is this?

        Login-exempt, like the other guest endpoints: the whole point of an
        invite is that somebody without an account can follow it. It answers
        with a room NAME and nothing else -- no member list, no media, no
        indication of whether the room is currently playing anything.

        Resolving deliberately does not consume a use. Burning the invite here
        would spend it on a page refresh, or on the visitor being bounced by
        the room's password prompt.
        """
        from .. import syncplay_rooms as sp
        room = sp.resolve_invite((request.args.get("code") or "").strip())
        if not room:
            return jsonify({"ok": False, "error": "expired or unknown"}), 404
        return jsonify({"ok": True, "room": room})

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
        from flask import session as _sess
        from .. import syncplay_rooms as sp
        if not _syncplay_enabled():
            return jsonify({"rooms": []})
        rooms = sp.list_rooms()
        # This endpoint is login-exempt so the lobby works for invited guests,
        # and it used to hand out the host's username and the title everyone is
        # watching to anyone who asked. Callers who are neither logged in nor a
        # room member get the bare directory: name, size, password flag.
        _known = _sess.get("user_id") is not None or sp.valid_token(_sess.get("sp_guest", ""))
        if not _known:
            rooms = [{"name": r["name"], "count": r["count"],
                      "has_password": r["has_password"], "locked": r["locked"]}
                     for r in rooms]
        return jsonify({"rooms": rooms})
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
