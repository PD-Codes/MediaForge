"""Watch-party reactions and invite links.

The reaction half is a regression test with a story: the reaction bar has been
in templates/syncplay.html since the room UI was written, posting to
``/api/syncplay/reaction`` — a route that did not exist. Every tap was a silent
404, and nothing in the UI said so, because a reaction that does not arrive
looks exactly like a reaction nobody else sent.
"""

import time

import pytest

from mediaforge.web import syncplay_rooms as sp


@pytest.fixture()
def room():
    token, _room, _snap = sp.join("pytest-party", "Host", is_guest=False)
    yield token
    try:
        sp.close_room(token)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------

def test_the_emoji_set_matches_the_buttons_in_the_template():
    """The bar and the server have to agree, or a tap is a silent no-op."""
    import pathlib
    import re

    template = (pathlib.Path(__file__).resolve().parents[1] / "src" / "mediaforge"
                / "web" / "templates" / "syncplay.html").read_text(encoding="utf-8")
    in_template = set(re.findall(r"SP\.react\('([^']+)'\)", template))
    assert in_template, "the reaction bar disappeared from the template"
    missing = in_template - set(sp.REACTIONS)
    assert not missing, "buttons the server would reject: %s" % missing


def test_a_reaction_reaches_the_room(room):
    queue = sp.subscribe(room)
    assert queue is not None
    assert sp.react(room, sp.REACTIONS[0]) is True

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    kinds = [e.get("type") for e in events]
    assert "reaction" in kinds
    reaction = [e for e in events if e.get("type") == "reaction"][0]
    assert reaction["emoji"] == sp.REACTIONS[0]
    assert reaction["name"] == "Host"


def test_free_text_is_refused(room):
    """An arbitrary string is rendered straight into everyone's player."""
    assert sp.react(room, "<img src=x onerror=alert(1)>") is False
    assert sp.react(room, "") is False
    assert sp.react(room, "not-an-emoji") is False


def test_reactions_are_not_chat(room):
    """A reaction is a moment, not a record. Twenty people tapping at a
    cliffhanger must not push the conversation out of the chat history."""
    for _ in range(5):
        sp.react(room, sp.REACTIONS[1])
    found = sp.room_for_token(room)
    assert found is not None
    assert len(found.chat) == 0


def test_rate_limited_per_member(room):
    ok = sum(1 for _ in range(sp.REACTION_BURST + 5) if sp.react(room, sp.REACTIONS[2]))
    assert ok == sp.REACTION_BURST

    # A second member has their own budget: one enthusiastic person must not
    # be able to silence everybody else.
    other, _r, _s = sp.join("pytest-party", "Guest", is_guest=True)
    assert sp.react(other, sp.REACTIONS[2]) is True


def test_unknown_token_cannot_react():
    assert sp.react("not-a-token", sp.REACTIONS[0]) is False


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

def test_host_can_invite_and_the_code_resolves(room):
    invite = sp.create_invite(room, minutes=30)
    assert invite and invite["room"] == "pytest-party"
    assert sp.resolve_invite(invite["code"]) == "pytest-party"


def test_guests_cannot_create_invites(room):
    """A guest handing out further invites is how a private room stops being
    one."""
    guest, _r, _s = sp.join("pytest-party", "Guest2", is_guest=True)
    assert sp.create_invite(guest) is None


def test_an_expired_invite_stops_resolving(room, monkeypatch):
    invite = sp.create_invite(room, minutes=1)
    assert sp.resolve_invite(invite["code"]) == "pytest-party"

    real_time = time.time
    monkeypatch.setattr(sp.time, "time", lambda: real_time() + 3600)
    assert sp.resolve_invite(invite["code"]) is None


def test_a_limited_invite_runs_out(room):
    invite = sp.create_invite(room, minutes=60, uses=2)
    assert invite["uses_left"] == 2
    assert sp.consume_invite(invite["code"]) is True
    assert sp.consume_invite(invite["code"]) is True
    # Spent: resolving must stop working.
    assert sp.resolve_invite(invite["code"]) is None
    assert sp.consume_invite(invite["code"]) is False


def test_resolving_does_not_consume(room):
    """Resolving happens on page load. Burning the invite there would spend it
    on a refresh, or on the visitor being bounced by a password prompt."""
    invite = sp.create_invite(room, minutes=60, uses=1)
    for _ in range(5):
        assert sp.resolve_invite(invite["code"]) == "pytest-party"
    assert sp.consume_invite(invite["code"]) is True
    assert sp.resolve_invite(invite["code"]) is None


def test_revoking_kills_a_link_immediately(room):
    invite = sp.create_invite(room, minutes=60)
    assert sp.revoke_invite(room, invite["code"]) is True
    assert sp.resolve_invite(invite["code"]) is None
    assert sp.revoke_invite(room, invite["code"]) is False


def test_live_invites_are_bounded(room):
    for _ in range(sp.MAX_INVITES):
        assert sp.create_invite(room, minutes=60) is not None
    assert sp.create_invite(room, minutes=60) is None


def test_lifetime_is_clamped(room):
    """A "1 minute" and a "ten years" invite are both configuration mistakes."""
    short = sp.create_invite(room, minutes=0)
    assert short["expires_at"] > time.time()
    long_lived = sp.create_invite(room, minutes=99999999)
    assert long_lived["expires_at"] < time.time() + 60 * 60 * 24 * 8


def test_unknown_code_resolves_to_nothing():
    assert sp.resolve_invite("nope") is None
    assert sp.resolve_invite("") is None
