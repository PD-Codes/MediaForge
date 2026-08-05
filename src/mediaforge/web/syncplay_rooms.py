"""Native in-app SyncPlay rooms — server-authoritative synchronised playback.

Unlike the old ``syncplay_bridge`` (which spoke the external Syncplay TCP
protocol to a third-party server), everything here lives inside the app. All
clients are browsers connected to the *same* instance — phone, tablet, PC — and
they talk to the server over plain HTTP + SSE. No TCP, no Twisted, no third
party.

Because the server holds the **canonical** playstate (position + a timestamp +
paused flag), a client that joins mid-stream is handed the exact current
position: ``position + (now - updated_at)`` while playing. The same authority
makes chat, ready-checks and "follow the host" trivial — we own both ends.

Design:
  * One ``Room`` per room name, holding ``Member`` objects.
  * The member who creates the room is the **host**; if the host leaves, the
    oldest remaining member inherits it.
  * Each member has a ``queue.Queue`` that the member's SSE stream drains. All
    state changes are broadcast as small JSON events.

The pure helpers (``effective_position``, ``snapshot`` builders) avoid I/O so
they can be unit-tested.

Used by: nearly every public function here is called from
``web/routes/syncplay.py`` (the HTTP/SSE endpoints for room actions).
``room_for_token`` is also used by ``web/routes/stream.py`` to derive a
shared-transcode key for SyncPlay viewers, and ``ensure_room`` is called
from ``web/app.py`` at startup to restore rooms saved before a restart.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from queue import Queue, Empty
from typing import Any

try:
    from ..logger import get_logger
    logger = get_logger(__name__)
except Exception:  # pragma: no cover - logging fallback
    import logging
    logger = logging.getLogger(__name__)

# Drop a member that has not sent a heartbeat / polled in this long.
MEMBER_TIMEOUT = 30.0
# Drop an empty room after this long (lets a host briefly reload without losing it).
ROOM_GRACE = 60.0
# Bounded chat history kept server-side for late joiners.
CHAT_HISTORY = 100

# Reactions a member may send within REACTION_WINDOW seconds. A reaction is a
# broadcast to everyone in the room, so this is a limit on how much noise one
# person can make, not a limit on enthusiasm.
REACTION_BURST = 8
REACTION_WINDOW = 10.0

# A closed set, and these are the exact characters templates/syncplay.html's
# reaction bar sends. Free text here would be a second chat with no length
# limit and no moderation, rendered into every participant's player -- and the
# client already floats whatever arrives straight into the video stage.
#
# The bar and the endpoint shipped without each other: the client has posted to
# /api/syncplay/reaction since the room UI was written, and no such route
# existed, so every tap was a silent 404. Keep the two in step.
REACTIONS = ("\U0001F44D", "\U0001F602", "\U0001F62E", "\U0001F622",
             "\U0001F525", "\u2764\uFE0F")

# Live invite links per room. A host who genuinely needs more simultaneous
# links than this is doing something other than inviting friends.
MAX_INVITES = 20
# Per-member event backlog before we force a full resync instead of leaking memory.
EVENT_BACKLOG = 500


class RoomError(Exception):
    """Join refused (banned, full, wrong password)."""


# ── Telemetry (stage 3: detail.syncplay) ────────────────────────────────────

def _participant_bracket(count) -> str:
    """Coarse participant-count category for detail.syncplay: "1", "2-4" or
    "5+". Deliberately a bracket rather than the exact number -- stage 3 is
    about how the feature is used, and an exact head count of a small
    household says more about who is watching than about the feature."""
    try:
        n = int(count)
    except (TypeError, ValueError):
        return "1"
    if n <= 1:
        return "1"
    if n <= 4:
        return "2-4"
    return "5+"


def report_session(status: str, count) -> None:
    """Submit one detail.syncplay event for a member's session lifecycle
    ("started" at join, "ended" when their participation stops).

    The payload carries the action, the status and the participant bracket and
    nothing else -- no room name, no member name, no token, no watched title
    (registry.py: "ohne Rauminhalt/Titel").

    Called once per join and once per member who leaves/is removed -- never
    from the SSE loop, which ticks every few seconds per connected member.

    MUST be called with no room / registry lock held. build_feature_detail_event()
    and client.submit() each read app_settings (consent + the enabled data keys)
    from SQLite, i.e. up to four reads that can block for the full busy_timeout
    while the queue worker writes. Callers therefore collect the values they need
    inside their ``with`` block and report afterwards -- see
    _report_sessions_ended(), which every multi-member path funnels through.

    A member disappearing because the browser tab was closed or the network
    dropped ENDS a session, it is not a failure: cancel-flavoured statuses are
    rejected outright below (via telemetry.classify) so no future caller can
    turn "the user walked away" into an error-looking data point.

    Fully guarded, imports included, so a telemetry problem can never disturb
    room bookkeeping: a leave/kick/close must not fail because telemetry did.
    """
    try:
        from ..telemetry import client as telemetry_client
        from ..telemetry import events as telemetry_events
        from ..telemetry.classify import is_cancel_status
        if is_cancel_status(status):
            return
        telemetry_client.submit(telemetry_events.build_feature_detail_event(
            "detail.syncplay", action="session", status=status,
            metadata={"participants_bracket": _participant_bracket(count)},
        ))
    except Exception:
        logger.debug("[Telemetry] failed to build/submit detail.syncplay event", exc_info=True)


def _report_sessions_ended(counts) -> None:
    """Report one "ended" session per entry in *counts*, after the caller has
    released its locks.

    The participant counts are gathered while the lock is still held, so each
    event still carries the bracket the room had at the moment that member's
    session actually ended -- only the DB-touching submit is deferred (see
    report_session() for why it must never run inside a lock).

    ``counts`` may be None/empty, which reports nothing. Each entry is submitted
    independently and report_session() swallows its own errors, so one bad event
    cannot cost the others.
    """
    for count in counts or ():
        report_session("ended", count)


def _room_content_provider(path):
    """Resolve which SITE a room's media file originally came from, in the
    spelling the telemetry adult guard uses ("hanime_tv"), or None.

    A room plays a LOCAL library file: the media dict the host announces is
    {title, is_movie, season, episode, file, subtitle} and carries no provider
    at all. The origin is recovered the same way routes/progress.py recovers it
    for watch events -- via the download_history row for that exact file -- but
    from ``series_url`` rather than the ``provider`` column: ``provider`` holds
    the HOSTER a download went through (VOE, Doodstream, "Direct"), which never
    identifies the age-gated site, while ``series_url`` is the page the content
    was actually taken from and maps cleanly through mirrors.site_for_url()
    (mirror domains included).

    Returns None whenever the origin cannot be established -- a file that was
    placed in the library by hand, imported via MediaScan, renamed, or
    downloaded before the history existed. The caller treats that as "do not
    send", NOT as "safe": an unknown file could be 18+ content, and for that
    provider nothing beyond flag.hanime_tv may ever leave the device.
    """
    path = (path or "").strip()
    if not path:
        return None
    from .db import get_db
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT series_url FROM download_history "
            "WHERE target_path = ? AND series_url IS NOT NULL AND series_url != '' "
            "ORDER BY id DESC LIMIT 1",
            (path,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    from ..mirrors import site_for_url
    site = site_for_url(row["series_url"])
    if not site:
        return None
    # mirrors/providers call the 18+ site "hanime"; telemetry's hard-coded
    # guard (sanitize.is_adult_provider) matches the literal "hanime_tv", so
    # translate here -- otherwise the guard silently never fires.
    return "hanime_tv" if site == "hanime" else site


def report_room_content(media) -> None:
    """Submit one syncplay.room_content event (stage 5) for the title a room
    just switched to.

    Called once per ACTUAL media change announced by the host -- not per SSE
    tick and not per participant (see set_media()/start_countdown(), which only
    call this when the file really changed).

    The provider is always passed truthfully, so build_play_event()'s
    is_adult_provider() guard can do its job; when the origin site cannot be
    determined the event is dropped here instead of being sent with
    provider=None, since that would be exactly the hole 18+ content could
    travel through.

    syncplay.room_content has its own consent toggle -- build_play_event()
    checks that key (not stream.play_events) and returns None when it is off.

    Fully guarded, imports and DB lookup included, so a telemetry problem can
    never disturb the room.
    """
    try:
        if not isinstance(media, dict):
            return
        provider = _room_content_provider(media.get("file"))
        if not provider:
            return
        from ..telemetry import client as telemetry_client
        from ..telemetry import events as telemetry_events
        telemetry_client.submit(telemetry_events.build_play_event(
            provider=provider,
            media_type="movie" if media.get("is_movie") else "series",
            title=media.get("title"),
            season=media.get("season"),
            episode=media.get("episode"),
            context="syncplay",
            data_key="syncplay.room_content",
        ))
    except Exception:
        logger.debug("[Telemetry] failed to build/submit syncplay.room_content event",
                     exc_info=True)


# ── Member ──────────────────────────────────────────────────────────────────

class Member:
    """One connected participant in a Room (host or guest)."""

    def __init__(self, token: str, name: str, is_guest: bool, device: str = "", ip: str = ""):
        self.token = token
        self.name = name
        self.is_guest = is_guest
        self.device = device or ""
        self.ip = ip or ""
        self.joined_at = time.time()
        self.last_seen = self.joined_at
        self.away = False
        self.file = None

        # Local playback state as last reported by this member's browser.
        self.position = 0.0
        self.paused = True
        self.buffering = False
        self.ready = True

        # Reaction timestamps, for the per-member rate limit. Per member and
        # not per room on purpose: a shared budget would let one enthusiastic
        # person drown out everybody else.
        self.reaction_times: list[float] = []

        # SSE delivery queue + overflow guard.
        self.q: "Queue[dict]" = Queue()
        self._queued = 0

    def public(self, host_token: str, room_file=None) -> dict:
        return {
            "name": self.name,
            "is_guest": self.is_guest,
            "device": self.device,
            "is_host": self.token == host_token,
            "ready": self.ready,
            "buffering": self.buffering,
            "paused": self.paused,
            "away": self.away,
            "different": bool(self.file and room_file and self.file != room_file),
            "initial": (self.name[:1] or "?").upper(),
        }

    def touch(self) -> None:
        self.last_seen = time.time()


# ── Room ────────────────────────────────────────────────────────────────────

class Room:
    """A single watch-party room: membership, canonical playstate, chat,
    history and moderation settings. All mutation happens under ``self.lock``."""

    def __init__(self, name: str):
        self.name = name
        self.created_at = time.time()
        self.host_token: str | None = None
        self.members: dict[str, Member] = {}
        self.chat: deque[dict] = deque(maxlen=CHAT_HISTORY)
        self._chat_seq = 0

        # Canonical playstate.
        self.paused = True
        self.position = 0.0
        self.updated_at = time.time()
        self.set_by = "Nobody"
        # True while playback is paused by a buffering/ready GATE (not a manual
        # pause). When the gate clears we auto-resume from here.
        self.gated = False

        # Currently selected media (announced by the host).
        self.media: dict | None = None
        self.history: list[dict] = []

        # Moderation / access control.
        self.banned_ips: set[str] = set()
        self.banned_names: set[str] = set()
        self.host_lock: bool = False           # only host may control playback
        self.max_members: int | None = None    # None = unlimited
        self.password: str | None = None

        # Invite links the host handed out. code -> {"expires_at", "uses_left",
        # "created_by"}. A link is a SEPARATE object from the room name on
        # purpose: sharing the room name is forever and cannot be taken back,
        # while an invite can be given a lifetime and revoked without renaming
        # the room out from under the people already in it.
        self.invites: dict[str, dict] = {}

        self.lock = threading.RLock()
        self._empty_since: float | None = None

    # -- playstate ----------------------------------------------------------
    def effective_position(self, now: float | None = None) -> float:
        """Authoritative position right now (advances while playing)."""
        if self.paused:
            return self.position
        now = now if now is not None else time.time()
        return self.position + max(0.0, now - self.updated_at)

    def _set_playstate(self, position: float, paused: bool, set_by: str) -> None:
        self.position = float(position)
        self.paused = bool(paused)
        self.updated_at = time.time()
        self.set_by = set_by

    # -- membership ---------------------------------------------------------
    def add_member(self, name: str, is_guest: bool, device: str = "", ip: str = "") -> Member:
        token = secrets.token_urlsafe(18)
        name = self._unique_name(name or ("Guest" if is_guest else "User"))
        m = Member(token, name, is_guest, device, ip)
        self.members[token] = m
        if self.host_token is None:
            self.host_token = token
        self._empty_since = None
        return m

    def _unique_name(self, name: str) -> str:
        existing = {m.name for m in self.members.values()}
        if name not in existing:
            return name
        i = 2
        while f"{name} ({i})" in existing:
            i += 1
        return f"{name} ({i})"

    def remove_member(self, token: str) -> int | None:
        """Drop a member from this room (host reassigned if needed).

        Returns the participant count at the moment of removal -- the room size
        the ending session still counted towards -- or None when *token* was not
        a member and nothing happened.

        The caller uses that number to report the "ended" session (mirroring the
        "started" reported in join(); covers leaving, being kicked/banned and
        being reaped after a dropped connection) AFTER releasing the room lock.
        This method deliberately does not report itself: it only ever runs with
        room.lock -- and for the reap/close paths with _registry_lock -- held,
        where a blocking SQLite read would stall every room (see
        report_session()).
        """
        m = self.members.pop(token, None)
        if not m:
            return None
        ended_count = len(self.members) + 1
        if token == self.host_token:
            # Transfer host to the longest-present remaining member.
            self.host_token = None
            if self.members:
                oldest = min(self.members.values(), key=lambda x: x.joined_at)
                self.host_token = oldest.token
        if not self.members:
            self._empty_since = time.time()
        return ended_count

    def is_expired(self, now: float | None = None) -> bool:
        if self.members:
            return False
        if self._empty_since is None:
            return False
        now = now if now is not None else time.time()
        return now - self._empty_since > ROOM_GRACE

    def reap_idle_members(self, now: float | None = None,
                          ended_counts: list | None = None) -> list[Member]:
        """Drop every member that stopped polling, returning those Members.

        ``ended_counts``: optional list that each removal's participant count is
        appended to, so the caller can report the ended sessions once it has left
        both locks -- see _reap(), which runs on every join() and every lobby
        poll while _registry_lock is held.
        """
        now = now if now is not None else time.time()
        dead = [t for t, m in self.members.items() if now - m.last_seen > MEMBER_TIMEOUT]
        dropped = []
        for t in dead:
            dropped.append(self.members[t])
            count = self.remove_member(t)
            if ended_counts is not None and count is not None:
                ended_counts.append(count)
        return dropped

    # -- readiness ----------------------------------------------------------
    def all_ready(self) -> bool:
        return all(m.ready and not m.buffering for m in self.members.values())

    # -- events -------------------------------------------------------------
    def broadcast(self, event: dict, exclude: str | None = None) -> None:
        for tok, m in self.members.items():
            if exclude is not None and tok == exclude:
                continue
            self._enqueue(m, event)

    def send_to(self, token: str, event: dict) -> None:
        m = self.members.get(token)
        if m:
            self._enqueue(m, event)

    def _enqueue(self, m: Member, event: dict) -> None:
        if m._queued > EVENT_BACKLOG:
            # The consumer fell too far behind — flush and tell it to resync.
            try:
                while True:
                    m.q.get_nowait()
            except Empty:
                pass
            m._queued = 0
            m.q.put({"type": "resync"})
            m._queued += 1
            return
        m.q.put(event)
        m._queued += 1

    # -- snapshots ----------------------------------------------------------
    def members_event(self) -> dict:
        rf = (self.media or {}).get("file")
        return {
            "type": "members",
            "members": [m.public(self.host_token or "", rf) for m in
                        sorted(self.members.values(), key=lambda x: x.joined_at)],
            "host_lock": self.host_lock,
            "max_members": self.max_members,
            "has_password": bool(self.password),
        }

    def state_event(self, kind: str = "sync") -> dict:
        return {
            "type": kind,
            "paused": self.paused,
            "position": self.effective_position(),
            "set_by": self.set_by,
        }

    def snapshot(self, token: str) -> dict:
        return {
            "room": self.name,
            "you": self.members[token].name if token in self.members else None,
            "is_host": token == self.host_token,
            "host": (self.members.get(self.host_token).name
                     if self.host_token and self.host_token in self.members else None),
            "members": self.members_event()["members"],
            "paused": self.paused,
            "position": self.effective_position(),
            "set_by": self.set_by,
            "media": self.media,
            "chat": list(self.chat),
            "all_ready": self.all_ready(),
            "host_lock": self.host_lock,
            "max_members": self.max_members,
            "has_password": bool(self.password),
            "history": self.history[-20:],
        }


# ── Registry ────────────────────────────────────────────────────────────────

_rooms: dict[str, Room] = {}
_registry_lock = threading.RLock()
# token -> room name, for fast lookup + guest-auth validation.
_token_index: dict[str, str] = {}


def _reap() -> None:
    # Reap idle *members* so counts stay accurate, but keep empty rooms alive so
    # people can rejoin them. Rooms are only removed by an explicit close.
    ended_counts: list[int] = []
    try:
        with _registry_lock:
            for name in list(_rooms.keys()):
                room = _rooms[name]
                with room.lock:
                    dropped = room.reap_idle_members(ended_counts=ended_counts)
                    if dropped:
                        for m in dropped:
                            _token_index.pop(m.token, None)
                        room.broadcast(room.members_event())
    finally:
        # Telemetry only after _registry_lock is gone: this runs on every join()
        # and every lobby poll, and report_session() reads app_settings from
        # SQLite. try/finally so an error inside the loop cannot drop reports
        # already collected.
        _report_sessions_ended(ended_counts)


def join(room_name: str, name: str, is_guest: bool, device: str = "",
         ip: str = "", password: str | None = None) -> tuple[str, Room, dict]:
    """Create/join a room. Returns (token, room, snapshot). Raises RoomError
    when access is refused (banned / full / wrong password)."""
    _reap()
    room_name = (room_name or "").strip()
    if not room_name:
        raise ValueError("room name required")
    desired = (name or "").strip()
    member_count = 0
    with _registry_lock:
        room = _rooms.get(room_name)
        is_new = room is None
        if is_new and is_guest:
            # Guests are *invited* to existing rooms — they may not create a new
            # room, nor resurrect one the host just closed. Only logged-in users
            # create rooms.
            raise RoomError("Dieser Raum existiert nicht (mehr).")
        if is_new:
            room = Room(room_name)
            _rooms[room_name] = room
        with room.lock:
            if not is_new:
                if ip and ip in room.banned_ips:
                    raise RoomError("Du wurdest aus diesem Raum gesperrt.")
                if desired and desired in room.banned_names:
                    raise RoomError("Dieser Name ist in diesem Raum gesperrt.")
                if room.password and (password or "") != room.password:
                    raise RoomError("Falsches Raum-Passwort.")
                if room.max_members and len(room.members) >= room.max_members:
                    raise RoomError("Der Raum ist voll.")
            member = room.add_member(name, is_guest, device, ip)
            _token_index[member.token] = room_name
            snap = room.snapshot(member.token)
            room.broadcast(room.members_event())
            member_count = len(room.members)
    # Telemetry: a session started for this member (the stage-2 counter
    # flag.syncplay is submitted by the /api/syncplay/join route).
    report_session("started", member_count)
    return member.token, room, snap


def get_snapshot(token: str) -> dict | None:
    """Return a fresh snapshot for a still-valid token (used to resume after a
    page reload), or None if the member/room no longer exists."""
    token = (token or "").strip()
    room = room_for_token(token)
    if not room:
        return None
    with room.lock:
        m = room.members.get(token)
        if not m:
            return None
        m.touch()
        return room.snapshot(token)


def room_for_token(token: str) -> Room | None:
    """Resolve a member token to its Room, or None if unknown/expired."""
    name = _token_index.get((token or "").strip())
    if not name:
        return None
    return _rooms.get(name)


def valid_token(token: str) -> bool:
    """True if ``token`` currently maps to a live room membership."""
    return (token or "").strip() in _token_index


def media_file_for_token(token: str) -> "str | None":
    """The file the token's room is currently watching, or None.

    A guest token proves membership in a room, not permission to read the whole
    library. This is what lets the stream endpoints check that a guest is
    asking for the file their room is actually playing.
    """
    room = room_for_token((token or "").strip())
    if not room:
        return None
    with room.lock:
        return (room.media or {}).get("file")


def leave(token: str) -> None:
    """Remove a member from their room and notify the rest (host reassigned
    automatically if needed — see ``Room.remove_member``)."""
    token = (token or "").strip()
    room = room_for_token(token)
    if not room:
        return
    ended_count = None
    try:
        with room.lock:
            name = room.members[token].name if token in room.members else None
            ended_count = room.remove_member(token)
            _token_index.pop(token, None)
            if name:
                room.broadcast({"type": "left", "name": name})
            room.broadcast(room.members_event())
    finally:
        # Outside the room lock, and in a finally so a failing broadcast cannot
        # swallow the report -- see report_session().
        _report_sessions_ended([ended_count] if ended_count is not None else None)


def control(token: str, action: str, position: float | None) -> bool:
    """Apply a play/pause/seek action from a member and broadcast the new
    playstate. Honors ``host_lock`` and the ready/buffering gate."""
    room = room_for_token(token)
    if not room:
        return False
    with room.lock:
        m = room.members.get(token)
        if not m:
            return False
        m.touch()
        if room.host_lock and token != room.host_token:
            # Only the host may drive playback in lock mode — snap them back.
            room.send_to(token, {"type": "denied", "reason": "host_lock"})
            room.send_to(token, room.state_event("sync"))
            return True
        pos = float(position) if position is not None else room.effective_position()
        if action == "play":
            # Gate playback until everyone is ready / done buffering.
            if not room.all_ready():
                room.gated = True
                room._set_playstate(pos, paused=True, set_by=m.name)
                room.broadcast({"type": "waiting", "position": pos, "set_by": m.name})
                return True
            room.gated = False
            room._set_playstate(pos, paused=False, set_by=m.name)
            # Record in history the first time this media actually plays.
            if room.media and (not room.history or
                               room.history[-1].get("file") != room.media.get("file")):
                _push_history(room, room.media)
                room.broadcast({"type": "history", "item": room.history[-1]})
            room.broadcast(room.state_event("play"))
        elif action == "pause":
            room.gated = False  # a manual pause cancels any pending gate
            room._set_playstate(pos, paused=True, set_by=m.name)
            room.broadcast(room.state_event("pause"))
        elif action == "seek":
            room._set_playstate(pos, paused=room.paused, set_by=m.name)
            ev = room.state_event("seek")
            ev["set_by"] = m.name
            room.broadcast(ev, exclude=token)
        else:
            return False
    return True


def report(token: str, position: float, paused: bool, buffering: bool = False,
           file: str | None = None) -> bool:
    """Record a member's locally-observed playback state; auto-pauses the
    room (gated) if someone starts buffering during playback."""
    room = room_for_token(token)
    if not room:
        return False
    with room.lock:
        m = room.members.get(token)
        if not m:
            return False
        m.touch()
        m.position = float(position)
        m.paused = bool(paused)
        if file is not None and file != m.file:
            m.file = file
            room.broadcast(room.members_event())
        was_buffering = m.buffering
        m.buffering = bool(buffering)
        # If someone starts buffering during playback, auto-pause the room.
        if m.buffering and not was_buffering and not room.paused:
            room.gated = True
            room._set_playstate(room.effective_position(), paused=True, set_by=m.name)
            room.broadcast({"type": "buffering", "name": m.name})
            room.broadcast(room.members_event())
        elif was_buffering and not m.buffering:
            room.broadcast(room.members_event())
            _try_resume_gate(room, m.name)
    return True


def set_ready(token: str, ready: bool) -> bool:
    """Mark a member ready/not-ready; resumes a gated room once everyone is."""
    room = room_for_token(token)
    if not room:
        return False
    with room.lock:
        m = room.members.get(token)
        if not m:
            return False
        m.touch()
        m.ready = bool(ready)
        room.broadcast(room.members_event())
        if room.all_ready():
            room.broadcast({"type": "all_ready"})
            _try_resume_gate(room, m.name)
    return True


def chat(token: str, text: str) -> bool:
    """Post a chat message (truncated to 2000 chars) to the room."""
    text = (text or "").strip()
    if not text:
        return False
    room = room_for_token(token)
    if not room:
        return False
    with room.lock:
        m = room.members.get(token)
        if not m:
            return False
        m.touch()
        room._chat_seq += 1
        msg = {"seq": room._chat_seq, "name": m.name, "text": text[:2000], "ts": time.time()}
        room.chat.append(msg)
        room.broadcast({"type": "chat", "message": msg})
    return True


def react(token: str, emoji: str) -> bool:
    """Send a reaction into the room.

    Deliberately NOT a chat message: a reaction is a moment, not a record.
    It is broadcast and forgotten -- never appended to ``room.chat`` -- so
    twenty people tapping the same emoji at a cliffhanger does not push the
    conversation off the top of the chat history, which is the one thing that
    would make people stop using either feature.

    The emoji set is closed. Free text here would be a second chat with no
    length limit and no moderation, and an arbitrary string rendered into
    everyone's player is not something to accept from a guest.
    """
    emoji = (emoji or "").strip()
    if emoji not in REACTIONS:
        return False
    room = room_for_token(token)
    if not room:
        return False
    with room.lock:
        member = room.members.get(token)
        if not member:
            return False
        member.touch()
        # Rate limited per member, not per room: one enthusiastic person must
        # not be able to drown out everybody else, and a shared limit would
        # let them do exactly that.
        now = time.time()
        recent = [t for t in member.reaction_times if now - t < REACTION_WINDOW]
        if len(recent) >= REACTION_BURST:
            member.reaction_times = recent
            return False
        recent.append(now)
        member.reaction_times = recent
        room.broadcast({"type": "reaction", "name": member.name,
                        "emoji": emoji, "ts": now})
    return True


# ---------------------------------------------------------------------------
# Invite links
# ---------------------------------------------------------------------------

def create_invite(host_token: str, *, minutes: int = 60,
                  uses: int | None = None) -> dict | None:
    """Host creates a shareable invite. Returns ``{"code", "expires_at", ...}``.

    Only the host: an invite is a decision about who gets into the room, and
    a guest handing out further invites is how a private room stops being one.
    """
    room = _host_room(host_token)
    if not room:
        return None
    minutes = max(1, min(int(minutes or 60), 60 * 24 * 7))
    if uses is not None:
        uses = max(1, min(int(uses), 100))

    with room.lock:
        # Bound the number of live invites. Without it a script could fill the
        # room object with codes, and a host who genuinely needs more than
        # this many simultaneous links is doing something else.
        room.invites = {c: i for c, i in room.invites.items()
                        if not _invite_expired(i)}
        if len(room.invites) >= MAX_INVITES:
            return None
        host = room.members.get(host_token)
        code = secrets.token_urlsafe(12)
        entry = {
            "expires_at": time.time() + minutes * 60,
            "uses_left": uses,
            "created_by": host.name if host else "?",
        }
        room.invites[code] = entry
    return {"code": code, "room": room.name,
            "expires_at": entry["expires_at"], "uses_left": entry["uses_left"]}


def _invite_expired(entry: dict, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    if entry.get("expires_at", 0) <= now:
        return True
    uses = entry.get("uses_left")
    return uses is not None and uses <= 0


def resolve_invite(code: str) -> str | None:
    """Room name for a live invite code, or None.

    Does NOT consume a use -- that happens in :func:`consume_invite`, once the
    join actually succeeds. Resolving on page load and consuming there would
    burn the invite on a refresh, or on the visitor being bounced by a
    password prompt.
    """
    code = (code or "").strip()
    if not code:
        return None
    with _registry_lock:
        rooms = list(_rooms.values())
    for room in rooms:
        with room.lock:
            entry = room.invites.get(code)
            if entry and not _invite_expired(entry):
                return room.name
    return None


def consume_invite(code: str) -> bool:
    """Decrement a limited invite. Called once a join has succeeded."""
    code = (code or "").strip()
    if not code:
        return False
    with _registry_lock:
        rooms = list(_rooms.values())
    for room in rooms:
        with room.lock:
            entry = room.invites.get(code)
            if not entry or _invite_expired(entry):
                continue
            if entry.get("uses_left") is not None:
                entry["uses_left"] -= 1
                if entry["uses_left"] <= 0:
                    room.invites.pop(code, None)
            return True
    return False


def list_invites(host_token: str) -> list[dict]:
    room = _host_room(host_token)
    if not room:
        return []
    with room.lock:
        room.invites = {c: i for c, i in room.invites.items()
                        if not _invite_expired(i)}
        return [{"code": code, "expires_at": entry["expires_at"],
                 "uses_left": entry["uses_left"]}
                for code, entry in room.invites.items()]


def revoke_invite(host_token: str, code: str) -> bool:
    room = _host_room(host_token)
    if not room:
        return False
    with room.lock:
        return room.invites.pop((code or "").strip(), None) is not None


def set_media(token: str, media: dict | None) -> bool:
    """Host announces the currently selected media / episode.

    Telemetry: reports syncplay.room_content once, and only when the room
    really switched to a different file -- re-picking what is already playing
    is not a new room content (see report_room_content()).
    """
    room = room_for_token(token)
    if not room:
        return False
    with room.lock:
        m = room.members.get(token)
        if not m:
            return False
        m.touch()
        if token != room.host_token:
            return False  # only the host drives media selection
        previous_file = (room.media or {}).get("file")
        room.media = media
        # New media → reset playstate to the start, paused. History is only
        # recorded once playback actually starts (see control()).
        room.gated = False
        room._set_playstate(0.0, paused=True, set_by=m.name)
        room.broadcast({"type": "media", "media": media, "set_by": m.name})
        changed = (media or {}).get("file") != previous_file
    # Outside the room lock on purpose: report_room_content() hits the download
    # history DB, which must not block the room's broadcasts.
    if changed:
        report_room_content(media)
    return True


def start_countdown(token: str, media: dict | None, seconds: int = 10) -> bool:
    """Host queues the next episode with a synced countdown for everyone.

    Same telemetry as set_media(): this is the other way the room's content
    changes (the auto-advance / "next episode" path), so it reports the new
    room content exactly once as well.
    """
    room = room_for_token(token)
    if not room:
        return False
    with room.lock:
        m = room.members.get(token)
        if not m:
            return False
        m.touch()
        if token != room.host_token:
            return False
        previous_file = (room.media or {}).get("file")
        room.media = media
        room.gated = False
        room._set_playstate(0.0, paused=True, set_by=m.name)
        room.broadcast({"type": "countdown", "media": media,
                        "countdown": max(3, int(seconds or 10)), "set_by": m.name})
        changed = (media or {}).get("file") != previous_file
    if changed:
        report_room_content(media)
    return True


def heartbeat(token: str) -> bool:
    """Keep a member's ``last_seen`` fresh so ``_reap`` doesn't drop them."""
    room = room_for_token(token)
    if not room:
        return False
    with room.lock:
        m = room.members.get(token)
        if not m:
            return False
        m.touch()
    return True


def subscribe(token: str) -> "Queue[dict] | None":
    """Return the member's event queue for an SSE stream to drain."""
    room = room_for_token(token)
    if not room:
        return None
    with room.lock:
        m = room.members.get(token)
        return m.q if m else None


def ack_drained(token: str, n: int) -> None:
    """Let a member's queue counter shrink as its SSE stream drains events."""
    room = room_for_token(token)
    if not room:
        return
    with room.lock:
        m = room.members.get(token)
        if m:
            m._queued = max(0, m._queued - n)


# ── Gate resume helper ───────────────────────────────────────────────────────

def _try_resume_gate(room: "Room", by_name: str) -> bool:
    """Resume playback that a buffering/ready gate paused, once the gate clears
    (everyone ready AND nobody buffering). Caller must hold ``room.lock``."""
    if room.gated and room.members and room.all_ready():
        room.gated = False
        room._set_playstate(room.effective_position(), paused=False, set_by=by_name)
        room.broadcast(room.state_event("play"))
        return True
    return False


# ── History helper ──────────────────────────────────────────────────────────

def _push_history(room: "Room", media: dict | None) -> None:
    """Append ``media`` to the room's watch history, deduping consecutive
    plays of the same file and capping the list at 100 entries."""
    if not media:
        return
    f = media.get("file")
    if room.history and room.history[-1].get("file") == f:
        return
    room.history.append({
        "title": media.get("title"), "subtitle": media.get("subtitle"),
        "poster": media.get("poster"), "file": f, "ts": time.time(),
    })
    if len(room.history) > 100:
        room.history = room.history[-100:]


def _find_by_name(room: "Room", name: str) -> "Member | None":
    """Look up a member by display name (moderation actions address members
    by name rather than token)."""
    for m in room.members.values():
        if m.name == name:
            return m
    return None


# ── Host moderation (host-only) ─────────────────────────────────────────────

def _host_room(token: str) -> "Room | None":
    """Return the room only if ``token`` belongs to its current host — guard
    used by all host-only moderation actions below."""
    room = room_for_token(token)
    if room and token == room.host_token:
        return room
    return None


def kick(host_token: str, target_name: str) -> bool:
    """Host-only: disconnect a member without banning them."""
    room = _host_room(host_token)
    if not room:
        return False
    ended_count = None
    try:
        with room.lock:
            t = _find_by_name(room, target_name)
            if not t or t.token == host_token:
                return False
            room.send_to(t.token, {"type": "kicked", "reason": "kick"})
            _token_index.pop(t.token, None)
            ended_count = room.remove_member(t.token)
            room.broadcast({"type": "left", "name": target_name})
            room.broadcast(room.members_event())
    finally:
        _report_sessions_ended([ended_count] if ended_count is not None else None)
    return True


def ban(host_token: str, target_name: str, by_ip: bool = True) -> bool:
    """Host-only: kick a member and blacklist their name (and IP by default)
    from rejoining this room."""
    room = _host_room(host_token)
    if not room:
        return False
    ended_count = None
    try:
        with room.lock:
            t = _find_by_name(room, target_name)
            if not t or t.token == host_token:
                return False
            room.banned_names.add(t.name)
            if by_ip and t.ip:
                room.banned_ips.add(t.ip)
            room.send_to(t.token, {"type": "kicked", "reason": "ban"})
            _token_index.pop(t.token, None)
            ended_count = room.remove_member(t.token)
            room.broadcast({"type": "left", "name": target_name})
            room.broadcast(room.members_event())
    finally:
        _report_sessions_ended([ended_count] if ended_count is not None else None)
    return True


def transfer_host(host_token: str, target_name: str) -> bool:
    """Host-only: hand host privileges to another member by name."""
    room = _host_room(host_token)
    if not room:
        return False
    with room.lock:
        t = _find_by_name(room, target_name)
        if not t:
            return False
        room.host_token = t.token
        room.broadcast({"type": "host", "name": t.name})
        room.broadcast(room.members_event())
    return True


def close_room(host_token: str) -> bool:
    """Host-only: disband the room immediately, evicting all members."""
    room = _host_room(host_token)
    if not room:
        return False
    ended_counts: list[int] = []
    try:
        with _registry_lock:
            with room.lock:
                room.broadcast({"type": "closed"})
                for tok in list(room.members.keys()):
                    _token_index.pop(tok, None)
                # Closing evicts everyone without going through remove_member(),
                # so collect one ended session per member here instead -- all of
                # them with the room size at close time.
                ending = len(room.members)
                ended_counts = [ending] * ending
                room.members.clear()
                _rooms.pop(room.name, None)
    finally:
        # Reported in one go after BOTH locks are released: a full room used to
        # mean one blocking DB read per member while _registry_lock was held,
        # stalling every join/leave/lobby poll of every room.
        _report_sessions_ended(ended_counts)
    return True


def set_host_lock(host_token: str, locked: bool) -> bool:
    """Host-only: toggle whether only the host may drive playback."""
    room = _host_room(host_token)
    if not room:
        return False
    with room.lock:
        room.host_lock = bool(locked)
        room.broadcast(room.members_event())
        room.broadcast({"type": "host_lock", "locked": room.host_lock})
    return True


def set_max(host_token: str, n: int | None) -> bool:
    """Host-only: cap member count (``n`` falsy/invalid = unlimited)."""
    room = _host_room(host_token)
    if not room:
        return False
    with room.lock:
        try:
            room.max_members = int(n) if n else None
        except (TypeError, ValueError):
            room.max_members = None
        room.broadcast(room.members_event())
    return True


def set_password(host_token: str, pw: str | None) -> bool:
    """Host-only: set or clear the room's join password."""
    room = _host_room(host_token)
    if not room:
        return False
    with room.lock:
        room.password = (pw or "").strip() or None
        room.broadcast(room.members_event())
    return True


# ── Presence / social ───────────────────────────────────────────────────────

def set_away(token: str, away: bool) -> bool:
    """Mark a member away/back, broadcasting only on an actual change."""
    room = room_for_token(token)
    if not room:
        return False
    with room.lock:
        m = room.members.get(token)
        if not m:
            return False
        m.touch()
        if m.away != bool(away):
            m.away = bool(away)
            room.broadcast(room.members_event())
    return True


def typing(token: str, is_typing: bool) -> bool:
    """Relay a chat typing-indicator to every other member (not persisted)."""
    room = room_for_token(token)
    if not room:
        return False
    with room.lock:
        m = room.members.get(token)
        if not m:
            return False
        m.touch()
        # Ephemeral — tell everyone *else* who is typing.
        for other_token, other in room.members.items():
            if other_token != token:
                room.send_to(other_token, {"type": "typing", "name": m.name,
                                           "typing": bool(is_typing)})
    return True


def reaction(token: str, emoji: str) -> bool:
    """Broadcast a short emoji reaction from a member (not persisted)."""
    emoji = (emoji or "").strip()[:8]
    if not emoji:
        return False
    room = room_for_token(token)
    if not room:
        return False
    with room.lock:
        m = room.members.get(token)
        if not m:
            return False
        m.touch()
        room.broadcast({"type": "reaction", "name": m.name, "emoji": emoji})
    return True


def set_track(host_token: str, kind: str, value) -> bool:
    """Host syncs playback rate / subtitle / audio track to everyone."""
    if kind not in ("rate", "subtitle", "audio"):
        return False
    room = _host_room(host_token)
    if not room:
        return False
    with room.lock:
        room.broadcast({"type": "track", "kind": kind, "value": value})
    return True


# ── Room directory / persistence ────────────────────────────────────────────

def list_rooms() -> list[dict]:
    """Public directory of all rooms (for the lobby browser)."""
    _reap()
    out = []
    with _registry_lock:
        for name, room in _rooms.items():
            with room.lock:
                host = room.members.get(room.host_token)
                out.append({
                    "name": name,
                    "count": len(room.members),
                    "watching": (room.media or {}).get("title"),
                    "watching_sub": (room.media or {}).get("subtitle"),
                    "host": host.name if host else None,
                    "has_password": bool(room.password),
                    "locked": room.host_lock,
                })
    out.sort(key=lambda r: (-r["count"], r["name"].lower()))
    return out


def close_by_name(name: str) -> bool:
    """Close/delete a room by name (used by the instance owner from the lobby)."""
    name = (name or "").strip()
    ended_counts: list[int] = []
    try:
        with _registry_lock:
            room = _rooms.get(name)
            if not room:
                return False
            with room.lock:
                room.broadcast({"type": "closed"})
                for tok in list(room.members.keys()):
                    _token_index.pop(tok, None)
                # Same as close_room(): members are dropped wholesale here, and
                # the reports are deferred until after both locks are released.
                ending = len(room.members)
                ended_counts = [ending] * ending
                room.members.clear()
                _rooms.pop(name, None)
    finally:
        _report_sessions_ended(ended_counts)
    return True


def all_room_names() -> list[str]:
    """All current room names (used to persist the room list across restarts)."""
    with _registry_lock:
        return list(_rooms.keys())


def ensure_room(name: str) -> None:
    """Pre-create an empty room (used to restore saved rooms on startup)."""
    name = (name or "").strip()
    if not name:
        return
    with _registry_lock:
        if name not in _rooms:
            _rooms[name] = Room(name)
