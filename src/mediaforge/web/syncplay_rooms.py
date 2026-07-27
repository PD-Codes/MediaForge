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
# Per-member event backlog before we force a full resync instead of leaking memory.
EVENT_BACKLOG = 500


class RoomError(Exception):
    """Join refused (banned, full, wrong password)."""


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

    def remove_member(self, token: str) -> None:
        m = self.members.pop(token, None)
        if not m:
            return
        if token == self.host_token:
            # Transfer host to the longest-present remaining member.
            self.host_token = None
            if self.members:
                oldest = min(self.members.values(), key=lambda x: x.joined_at)
                self.host_token = oldest.token
        if not self.members:
            self._empty_since = time.time()

    def is_expired(self, now: float | None = None) -> bool:
        if self.members:
            return False
        if self._empty_since is None:
            return False
        now = now if now is not None else time.time()
        return now - self._empty_since > ROOM_GRACE

    def reap_idle_members(self, now: float | None = None) -> list[Member]:
        now = now if now is not None else time.time()
        dead = [t for t, m in self.members.items() if now - m.last_seen > MEMBER_TIMEOUT]
        dropped = []
        for t in dead:
            dropped.append(self.members[t])
            self.remove_member(t)
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
    with _registry_lock:
        for name in list(_rooms.keys()):
            room = _rooms[name]
            with room.lock:
                dropped = room.reap_idle_members()
                if dropped:
                    for m in dropped:
                        _token_index.pop(m.token, None)
                    room.broadcast(room.members_event())


def join(room_name: str, name: str, is_guest: bool, device: str = "",
         ip: str = "", password: str | None = None) -> tuple[str, Room, dict]:
    """Create/join a room. Returns (token, room, snapshot). Raises RoomError
    when access is refused (banned / full / wrong password)."""
    _reap()
    room_name = (room_name or "").strip()
    if not room_name:
        raise ValueError("room name required")
    desired = (name or "").strip()
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
    with room.lock:
        name = room.members[token].name if token in room.members else None
        room.remove_member(token)
        _token_index.pop(token, None)
        if name:
            room.broadcast({"type": "left", "name": name})
        room.broadcast(room.members_event())


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


def set_media(token: str, media: dict | None) -> bool:
    """Host announces the currently selected media / episode."""
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
        room.media = media
        # New media → reset playstate to the start, paused. History is only
        # recorded once playback actually starts (see control()).
        room.gated = False
        room._set_playstate(0.0, paused=True, set_by=m.name)
        room.broadcast({"type": "media", "media": media, "set_by": m.name})
    return True


def start_countdown(token: str, media: dict | None, seconds: int = 10) -> bool:
    """Host queues the next episode with a synced countdown for everyone."""
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
        room.media = media
        room.gated = False
        room._set_playstate(0.0, paused=True, set_by=m.name)
        room.broadcast({"type": "countdown", "media": media,
                        "countdown": max(3, int(seconds or 10)), "set_by": m.name})
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
    with room.lock:
        t = _find_by_name(room, target_name)
        if not t or t.token == host_token:
            return False
        room.send_to(t.token, {"type": "kicked", "reason": "kick"})
        _token_index.pop(t.token, None)
        room.remove_member(t.token)
        room.broadcast({"type": "left", "name": target_name})
        room.broadcast(room.members_event())
    return True


def ban(host_token: str, target_name: str, by_ip: bool = True) -> bool:
    """Host-only: kick a member and blacklist their name (and IP by default)
    from rejoining this room."""
    room = _host_room(host_token)
    if not room:
        return False
    with room.lock:
        t = _find_by_name(room, target_name)
        if not t or t.token == host_token:
            return False
        room.banned_names.add(t.name)
        if by_ip and t.ip:
            room.banned_ips.add(t.ip)
        room.send_to(t.token, {"type": "kicked", "reason": "ban"})
        _token_index.pop(t.token, None)
        room.remove_member(t.token)
        room.broadcast({"type": "left", "name": target_name})
        room.broadcast(room.members_event())
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
    with _registry_lock:
        with room.lock:
            room.broadcast({"type": "closed"})
            for tok in list(room.members.keys()):
                _token_index.pop(tok, None)
            room.members.clear()
            _rooms.pop(room.name, None)
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
    with _registry_lock:
        room = _rooms.get(name)
        if not room:
            return False
        with room.lock:
            room.broadcast({"type": "closed"})
            for tok in list(room.members.keys()):
                _token_index.pop(tok, None)
            room.members.clear()
            _rooms.pop(name, None)
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
