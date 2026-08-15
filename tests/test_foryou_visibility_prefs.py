"""'Could be for you' visibility, split into hero banner and rail.

Two independent per-account prefs (foryou_hero_hidden, foryou_hidden -- the
latter kept its original name/meaning, now scoped to just the rail, see
static/home_foryou.js's heroHidden()/railHidden()) plus their instance-default
counterparts an admin sets under Settings -> Start Page.
"""


def test_foryou_hero_hidden_pref_accepts_bool_strings_rejects_junk(as_user):
    ok = as_user("user").post("/api/user/preferences", json={"foryou_hero_hidden": "1"})
    assert ok.status_code == 200
    ok2 = as_user("user").post("/api/user/preferences", json={"foryou_hero_hidden": "0"})
    assert ok2.status_code == 200
    bad = as_user("user").post("/api/user/preferences", json={"foryou_hero_hidden": "maybe"})
    assert bad.status_code >= 400


def test_foryou_rail_pref_still_works_unchanged(as_user):
    """foryou_hidden predates the hero/rail split -- its key and validator
    must not have moved under existing accounts."""
    ok = as_user("user").post("/api/user/preferences", json={"foryou_hidden": "1"})
    assert ok.status_code == 200


def test_foryou_instance_defaults_round_trip(as_user):
    admin = as_user("admin")
    try:
        resp = admin.put("/api/settings", json={
            "foryou_hero_hidden_default": "1",
            "foryou_hidden_default": "1",
        })
        assert resp.status_code == 200
        data = admin.get("/api/settings").get_json()
        assert data["foryou_hero_hidden_default"] == "1"
        assert data["foryou_hidden_default"] == "1"
    finally:
        admin.put("/api/settings", json={
            "foryou_hero_hidden_default": "",
            "foryou_hidden_default": "",
        })


def test_foryou_instance_default_junk_is_dropped_not_stored(as_user):
    admin = as_user("admin")
    admin.put("/api/settings", json={"foryou_hero_hidden_default": "yes-please"})
    data = admin.get("/api/settings").get_json()
    # An unrecognised value falls back to "" rather than being stored as-is
    # (see routes/settings.py's PUT handler) -- the client only ever sends
    # "", "0" or "1", so anything else is a fresher-request race or a
    # malformed request, not a value MediaForge invented and needs to keep.
    assert data["foryou_hero_hidden_default"] == ""
