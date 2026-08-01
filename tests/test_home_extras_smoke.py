"""Home page 2.1: the endpoints around the rows.

Covers the parts that are easy to get subtly wrong and impossible to notice
by looking at the page: the per-row feed endpoint, the age ceiling actually
being a server-side restriction rather than a client-side filter, and the
media-server client refusing an id it did not hand out.
"""

import pytest


# ---------------------------------------------------------------------------
# Per-row feed (#13)
# ---------------------------------------------------------------------------

def test_each_discovery_row_can_be_fetched_on_its_own(as_user, monkeypatch):
    from mediaforge.web.routes import browse
    # No network: the fetchers are the sites themselves, and this test is
    # about the shape of the answer, not about what is on AniWorld today.
    monkeypatch.setattr(browse, "_cached_browse", lambda key, fn: [])
    client = as_user("user")
    for row in ("new", "popular", "movies"):
        data = client.get("/api/home-feed/row/" + row).get_json()
        assert data["row"] == row
        assert row in data["rows"]
        # The chip row is built from whichever row answers first, so every
        # response has to carry the source list -- not just the first one.
        assert isinstance(data["sources"], list)


def test_an_unknown_row_is_404_not_an_empty_page(as_user):
    assert as_user("user").get("/api/home-feed/row/nonsense").status_code == 404


def test_a_row_request_does_not_fetch_the_other_rows(as_user, monkeypatch):
    """The whole point of the split: asking for "popular" must not scrape the
    sites that only publish "new"."""
    from mediaforge.web.routes import browse

    asked = []

    def _fake_cached_browse(key, fn):
        asked.append(key)
        return []

    monkeypatch.setattr(browse, "_cached_browse", _fake_cached_browse)
    as_user("user").get("/api/home-feed/row/popular")
    assert asked, "no source was consulted at all"
    assert all("new" not in key for key in asked)


# ---------------------------------------------------------------------------
# Age ceiling (#8) -- the part that has to be a restriction, not a filter
# ---------------------------------------------------------------------------

def _pin_of(_set_setting):
    """The PIN currently configured -- the teardown of the two tests below
    needs it to get back out of the mode they entered."""
    from mediaforge.web.db import get_setting
    return (get_setting("home_kids_pin", "") or "").strip()


def test_entering_kids_mode_is_refused_until_an_admin_armed_it(as_user):
    """A mode with no PIN behind it is one nobody could ever leave, so it is
    refused rather than entered."""
    from mediaforge.web.db import set_setting
    set_setting("home_kids_enabled", "0")
    set_setting("home_kids_pin", "")
    resp = as_user("user").post("/api/home/mode",
                                json={"mode": "kids", "max_fsk": "6"})
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "kids-disabled"


def test_the_age_ceiling_is_not_writable_through_the_generic_prefs_endpoint(as_user):
    """A kids mode a client can switch off with one PUT is decoration."""
    resp = as_user("user").post("/api/user/preferences", json={"home_max_fsk": "18"})
    assert resp.status_code >= 400


def test_lowering_the_ceiling_is_free_and_raising_it_needs_the_pin(as_user):
    from mediaforge.web.db import set_setting

    client = as_user("user")
    set_setting("home_kids_pin", "1234")
    set_setting("home_kids_enabled", "1")
    try:
        assert client.post("/api/home/mode",
                           json={"mode": "kids", "max_fsk": "6"}).status_code == 200
        # Back out again without the PIN: refused.
        assert client.post("/api/home/mode",
                           json={"mode": "", "max_fsk": ""}).status_code == 403
        # With it: allowed.
        assert client.post("/api/home/mode",
                           json={"mode": "", "max_fsk": "", "pin": "1234"}).status_code == 200
    finally:
        client.post("/api/home/mode", json={"mode": "", "max_fsk": "",
                                            "pin": _pin_of(set_setting)})
        set_setting("home_kids_pin", "")
        set_setting("home_kids_enabled", "0")


def test_a_wrong_pin_does_not_leak_through_a_lower_step(as_user):
    """"" (no ceiling) is the HIGHEST value there is, not the lowest -- a
    plain string comparison gets that exactly backwards."""
    from mediaforge.web.db import set_setting

    client = as_user("user")
    set_setting("home_kids_pin", "4321")
    set_setting("home_kids_enabled", "1")
    try:
        client.post("/api/home/mode", json={"mode": "kids", "max_fsk": "0"})
        assert client.post("/api/home/mode",
                           json={"max_fsk": "", "pin": "0000"}).status_code == 403
        assert client.post("/api/home/mode",
                           json={"max_fsk": "12", "pin": "0000"}).status_code == 403
    finally:
        client.post("/api/home/mode", json={"mode": "", "max_fsk": "",
                                            "pin": _pin_of(set_setting)})
        set_setting("home_kids_pin", "")
        set_setting("home_kids_enabled", "0")


def test_cards_above_the_ceiling_are_dropped_but_unrated_ones_are_kept():
    """Dropping everything TMDB has no rating for would empty the page on an
    instance without a TMDB key, and teach people to switch the limit off."""
    from mediaforge.web.routes.browse import _feed_apply_age_limit

    items = [
        {"title": "kids", "tmdb": {"fsk": "6"}},
        {"title": "teens", "tmdb": {"fsk": "16"}},
        {"title": "unrated", "tmdb": {}},
        {"title": "nothing at all"},
    ]
    kept = [i["title"] for i in _feed_apply_age_limit(items, 12)]
    assert kept == ["kids", "unrated", "nothing at all"]
    # No ceiling -> the list is handed back untouched.
    assert _feed_apply_age_limit(items, None) is items


# ---------------------------------------------------------------------------
# Media server (#continue watching / #15)
# ---------------------------------------------------------------------------

def test_an_unlinked_id_is_refused_before_it_reaches_a_url(monkeypatch):
    """The id comes from a per-user preference, so without this check an
    account could read anybody's history by editing it."""
    from mediaforge.web import mediaplayer

    monkeypatch.setattr(mediaplayer, "config",
                        lambda: {"kind": "jellyfin", "url": "http://x", "token": "t"})
    monkeypatch.setattr(mediaplayer, "list_users", lambda: [{"id": "abc", "name": "A"}])

    def _boom(*args, **kwargs):
        raise AssertionError("the server was contacted for an unknown user")

    monkeypatch.setattr(mediaplayer, "_get_json", _boom)
    assert mediaplayer.continue_watching("../../etc") == []
    assert mediaplayer.watch_stats("someone-else", 0) == {"available": False}


def test_artwork_proxy_refuses_anything_that_is_not_server_relative(monkeypatch):
    from mediaforge.web import mediaplayer

    monkeypatch.setattr(mediaplayer, "config",
                        lambda: {"kind": "plex", "url": "http://x", "token": "t"})
    for path in ("//evil.example/x.jpg", "http://evil.example/x.jpg", "",
                 "\\evil", "/ok\\..\\x"):
        assert mediaplayer.image_bytes(path) == (None, None)


@pytest.mark.parametrize("value,expected", [
    ("2026-07-31T22:10:05.1234567Z", True),     # Jellyfin's seven digits
    ("2026-07-31T22:10:05Z", True),
    ("2026-07-31T22:10:05.123+02:00", True),
    ("not a date", False),
    ("", False),
])
def test_jellyfin_timestamps_survive_the_parser(value, expected):
    """datetime.fromisoformat() accepts at most six fractional digits and
    raises on seven -- which is exactly what Jellyfin emits."""
    from mediaforge.web.mediaplayer import _parse_iso
    assert bool(_parse_iso(value)) is expected


# ---------------------------------------------------------------------------
# Onboarding / suggestions / wrapped
# ---------------------------------------------------------------------------

def test_onboarding_hides_admin_only_steps_from_a_normal_account(as_user):
    user_steps = {s["key"] for s in
                  as_user("user").get("/api/home/onboarding").get_json()["steps"]}
    admin_steps = {s["key"] for s in
                   as_user("admin").get("/api/home/onboarding").get_json()["steps"]}
    assert "sources" in admin_steps and "sources" not in user_steps
    assert "modules" in user_steps


def test_suggest_says_nothing_until_there_is_something_to_say(as_user):
    data = as_user("user").get("/api/home/suggest?q=a").get_json()
    assert data["groups"] == []


def test_wrapped_defaults_to_the_month_that_just_ended(as_user):
    data = as_user("user").get("/api/home/wrapped").get_json()
    assert len(data["period"]) == 7 and data["period"][4] == "-"
    # No media server linked -> the watched half is absent rather than zeroed.
    assert data["watched"]["available"] is False
    assert data["downloaded"]["count"] == 0


def test_wrapped_rejects_a_junk_period_instead_of_trusting_it(as_user):
    data = as_user("user").get("/api/home/wrapped?period=9999-99").get_json()
    assert len(data["period"]) == 7


def test_every_onboarding_step_points_at_a_real_place(as_user):
    """A checklist that links to a tab that does not exist is worse than no
    checklist -- the page loads and simply does not scroll anywhere. The
    media-server step pointed at /settings#account, which is neither a tab
    nor a page a normal account may open."""
    steps = {s["key"]: s["link"]
             for s in as_user("admin").get("/api/home/onboarding").get_json()["steps"]}
    assert steps["sources"] == "/settings#sources"
    assert steps["library"] == "/settings#library"
    # The TMDB key lives on Integrations; Settings has no CineInfo tab at all.
    assert steps["tmdb"] == "/integrations#cineinfo"
    assert steps["modules"] == "/extensions"
    assert steps["mediaplayer"] == "/profile#mediaplayer"


def test_a_module_card_says_it_comes_from_a_module(app):
    """On a shared tab a module's card sits next to the built-in ones and
    looked exactly like them."""
    from mediaforge.web.thirdparties.registry import _build_card

    with app.test_request_context():
        card = _build_card({
            "id": "not-registered-by-any-module", "label": "X", "badges": [],
            "description": "", "enable_label": "on", "enable_desc": "",
        })
    assert card["module_name"] is None      # a built-in gets no pill
