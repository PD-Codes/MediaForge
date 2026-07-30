"""Home panel bar: /api/home-panels and /api/home-panel/<id>.

The interesting parts are not "does it return JSON" but the three promises the
feature makes: an admin-only panel is invisible AND unreachable for a normal
account, one broken panel does not take the page down, and nothing a module
returns reaches the DOM unfiltered.
"""

import pytest


@pytest.fixture()
def clean_registry():
    """Registered panels are process-global -- a test that leaves one behind
    changes what the next test sees in the bar."""
    from mediaforge import home_panels as HP
    HP._EXTRA_HOME_PANELS.clear()
    yield HP
    HP._EXTRA_HOME_PANELS.clear()


@pytest.fixture(autouse=True)
def fresh_badges():
    """The badge cache is per process and deliberately shared across accounts;
    without this the second test in a run reads the first one's numbers."""
    from mediaforge.web.routes import home_panels as R
    R._badge_cache["at"] = 0.0
    R._badge_cache["values"].clear()
    yield


# ── the bar ──────────────────────────────────────────────────────────────

def test_user_does_not_see_admin_only_panels(as_user):
    data = as_user("user").get("/api/home-panels").get_json()
    ids = [p["id"] for p in data["panels"]]
    assert "queue" in ids and "library" in ids
    assert "storage" not in ids and "system" not in ids


def test_admin_sees_every_builtin_panel(as_user):
    data = as_user("admin").get("/api/home-panels").get_json()
    ids = [p["id"] for p in data["panels"]]
    assert {"queue", "activity", "library", "storage", "system"} <= set(ids)


def test_admin_only_panel_is_forbidden_not_just_hidden(as_user):
    """The button being absent is cosmetic; the route is the actual gate."""
    assert as_user("user").get("/api/home-panel/storage").status_code == 403
    assert as_user("admin").get("/api/home-panel/storage").status_code == 200


def test_unknown_panel_is_404(as_user):
    assert as_user("user").get("/api/home-panel/nope").status_code == 404


def test_panel_bodies_have_the_expected_shape(as_user):
    for pid in ("queue", "activity", "library"):
        body = as_user("user").get("/api/home-panel/" + pid).get_json()
        assert body["id"] == pid
        assert isinstance(body["stats"], list)
        assert isinstance(body["items"], list)
        assert "error" not in body, body


# ── module panels ────────────────────────────────────────────────────────

def test_module_panel_shows_up_and_renders(as_user, clean_registry):
    clean_registry.register_home_panel(
        "demo-module", "demo", "Demo",
        view=lambda: {"items": [{"title": "hello", "href": "/library"}]},
        badge=lambda: 3)
    data = as_user("user").get("/api/home-panels").get_json()
    entry = next(p for p in data["panels"] if p["id"] == "demo")
    assert entry["badge"] == 3 and entry["builtin"] is False
    body = as_user("user").get("/api/home-panel/demo").get_json()
    assert body["items"][0]["title"] == "hello"


def test_a_broken_panel_reports_itself_instead_of_500(as_user, clean_registry):
    def boom():
        raise RuntimeError("module is having a day")

    clean_registry.register_home_panel("bad-module", "bad", "Bad", view=boom)
    resp = as_user("user").get("/api/home-panel/bad")
    assert resp.status_code == 200
    assert resp.get_json()["error"]


def test_a_broken_badge_does_not_break_the_bar(as_user, clean_registry):
    def boom():
        raise RuntimeError("nope")

    clean_registry.register_home_panel("bad-module", "bad", "Bad",
                                       view=lambda: {}, badge=boom)
    data = as_user("user").get("/api/home-panels").get_json()
    assert next(p for p in data["panels"] if p["id"] == "bad")["badge"] == 0


def test_module_panel_output_is_rebuilt_field_by_field(as_user, clean_registry):
    """A module hands back a dict; only the keys the client knows survive, and
    an off-site href is dropped rather than rendered as a link."""
    clean_registry.register_home_panel(
        "demo-module", "demo", "Demo",
        view=lambda: {
            "items": [
                {"title": "a", "href": "https://evil.example", "percent": 999},
                {"title": "b", "href": "//evil.example"},
                {"title": "c", "href": "/queue", "onclick": "alert(1)"},
            ],
            "link": {"href": "javascript:alert(1)", "label": "go"},
            "surprise": "<script>",
        })
    body = as_user("user").get("/api/home-panel/demo").get_json()
    assert body["items"][0]["href"] == ""          # absolute URL dropped
    assert body["items"][1]["href"] == ""          # protocol-relative dropped
    assert body["items"][2]["href"] == "/queue"
    assert body["items"][0]["percent"] == 100      # clamped, not passed through
    assert "onclick" not in body["items"][2]
    assert body["link"] is None
    assert "surprise" not in body


def test_panel_item_count_is_capped(as_user, clean_registry):
    from mediaforge.home_panels import PANEL_MAX_ITEMS
    clean_registry.register_home_panel(
        "demo-module", "demo", "Demo",
        view=lambda: {"items": [{"title": str(i)} for i in range(200)]})
    body = as_user("user").get("/api/home-panel/demo").get_json()
    assert len(body["items"]) == PANEL_MAX_ITEMS


def test_admin_only_module_panel_follows_the_same_rule(as_user, clean_registry):
    clean_registry.register_home_panel("demo-module", "demo", "Demo",
                                       view=lambda: {}, admin_only=True)
    assert as_user("user").get("/api/home-panel/demo").status_code == 403
    ids = [p["id"] for p in as_user("user").get("/api/home-panels").get_json()["panels"]]
    assert "demo" not in ids


# ── registry rules ───────────────────────────────────────────────────────

def test_a_module_cannot_shadow_a_builtin(clean_registry):
    with pytest.raises(ValueError):
        clean_registry.register_home_panel("m", "queue", "Queue", view=lambda: {})


def test_two_modules_cannot_share_a_panel_id(clean_registry):
    clean_registry.register_home_panel("m1", "demo", "Demo", view=lambda: {})
    with pytest.raises(ValueError):
        clean_registry.register_home_panel("m2", "demo", "Demo", view=lambda: {})


def test_unregister_drops_the_panel(clean_registry):
    clean_registry.register_home_panel("m1", "demo", "Demo", view=lambda: {})
    assert clean_registry.thirdparty_home_panel_ids() == {"m1"}
    clean_registry.unregister_home_panel("m1")
    assert clean_registry.thirdparty_home_panel_ids() == set()


def test_icon_data_is_filtered(clean_registry):
    clean_registry.register_home_panel("m1", "a", "A", view=lambda: {},
                                       icon='"/><script>alert(1)</script>')
    clean_registry.register_home_panel("m2", "b", "B", view=lambda: {},
                                       icon="M3 6h18")
    panels = {p["panel_id"]: p["icon"] for p in clean_registry.iter_home_panels()}
    assert panels["a"] == ""
    assert panels["b"] == "M3 6h18"


def test_module_cleanup_removes_panels():
    """unregister_module() must drop panels too -- otherwise a disabled
    module's button stays in the bar and 500s when clicked."""
    from mediaforge.web.thirdparties import registry as R
    import inspect
    source = inspect.getsource(R.unregister_module)
    assert "unregister_home_panel" in source


# ── the stored panel ─────────────────────────────────────────────────────

def test_stored_panel_is_only_returned_when_still_visible(as_user, app):
    from mediaforge.web import db
    client = as_user("user")
    with client.session_transaction() as sess:
        uid = sess["user_id"]
    with app.app_context():
        db.set_user_ui_prefs(uid, {"home_panel": "storage"})   # admin-only
    assert as_user("user").get("/api/home-panels").get_json()["active"] == ""
    with app.app_context():
        db.set_user_ui_prefs(uid, {"home_panel": "queue"})
    assert as_user("user").get("/api/home-panels").get_json()["active"] == "queue"

# ── the queue is a modal, not a page ─────────────────────────────────────

def test_queue_panel_uses_an_action_and_never_links_to_a_missing_route(as_user, app):
    """There is no /queue route -- the queue hub is a modal in base.html. A
    link there produced a 404, which is the bug this pins."""
    body = as_user("user").get("/api/home-panel/queue").get_json()
    assert body["link"]["action"] == "queue"
    assert not body["link"]["href"]
    assert all(not i["href"] for i in body["items"])
    assert all(i["action"] == "queue" for i in body["items"])
    # and the route really is absent, so nobody "fixes" this by adding an href
    assert not any(str(r.rule) == "/queue" for r in app.url_map.iter_rules())


def test_only_known_actions_survive(as_user, clean_registry):
    clean_registry.register_home_panel(
        "demo-module", "demo", "Demo",
        view=lambda: {"items": [{"title": "a", "action": "eval"},
                                {"title": "b", "action": "queue"}],
                      "link": {"action": "nope", "label": "x"}})
    body = as_user("user").get("/api/home-panel/demo").get_json()
    assert body["items"][0]["action"] == ""
    assert body["items"][1]["action"] == "queue"
    assert body["link"] is None


# ── library cache shape ──────────────────────────────────────────────────

def test_library_panel_reads_the_cache_dict_not_its_keys(as_user, app, monkeypatch):
    """entry["data"] is a dict; iterating it yields key strings. That made the
    panel report "1 title, 1 series" on a library with hundreds of both."""
    from mediaforge.web.routes import home_panels as R

    entry = {"data": {
        "label": "Default", "custom_path_id": None, "titles": [
            {"folder": "Serie A", "is_movie": False, "total_episodes": 12,
             "total_size": 1024 ** 3},
            {"folder": "Film B", "is_movie": True, "total_size": 2 * 1024 ** 3},
        ],
        "books": [],
    }}
    monkeypatch.setattr("mediaforge.web.db.get_all_library_cache",
                        lambda: {"default": entry})
    monkeypatch.setattr("mediaforge.web.routes.library._lib_active_path_keys",
                        lambda: {"default"})
    with app.test_request_context():
        stats = {s["label_key"]: s["value"] for s in R._panel_library()["stats"]}
    assert stats["hp_series"] == "1"
    assert stats["hp_movies"] == "1"
    assert stats["hp_episodes"] == "12"
    assert stats["hp_size"].startswith("3.0 GB")


def test_language_separated_libraries_are_counted_too(app, monkeypatch):
    """With language separation on, `titles` is None and everything hides in
    lang_folders -- which no consumer outside library.py handled."""
    from mediaforge.web.routes import home_panels as R

    entry = {"data": {"titles": None, "lang_folders": [
        {"name": "German", "titles": [{"folder": "S1", "is_movie": False,
                                       "total_episodes": 3, "total_size": 0}]},
        {"name": "English", "titles": [{"folder": "S2", "is_movie": False,
                                        "total_episodes": 4, "total_size": 0}]},
    ]}}
    monkeypatch.setattr("mediaforge.web.db.get_all_library_cache",
                        lambda: {"default": entry})
    monkeypatch.setattr("mediaforge.web.routes.library._lib_active_path_keys",
                        lambda: {"default"})
    with app.test_request_context():
        stats = {s["label_key"]: s["value"] for s in R._panel_library()["stats"]}
    assert stats["hp_series"] == "2"
    assert stats["hp_episodes"] == "7"
