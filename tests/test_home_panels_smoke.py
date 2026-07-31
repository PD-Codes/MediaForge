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


# ── badges count the queue, not its archive ──────────────────────────────

def _queue_row(status, hidden):
    """One queue row through the real API, then forced into the state we want.

    Deliberately not a hand-written INSERT: download_queue has a dozen NOT
    NULL columns and a test that spells them out itself goes red the next
    time one is added, for a reason that has nothing to do with badges.
    """
    from mediaforge.web import db as DB
    qid = DB.add_to_queue("T", "https://example.invalid/x", [{"episode": 1}],
                          "German", "aniworld")
    conn = DB.get_db()
    try:
        conn.execute("UPDATE download_queue SET status = ?, hidden = ? WHERE id = ?",
                     (status, hidden, qid))
        conn.commit()
    finally:
        conn.close()


def test_cleared_entries_stop_counting_towards_the_badges(app):
    """Removing a finished entry sets hidden = 1 instead of deleting the row,
    so the download still counts towards the statistics. The badges must NOT
    follow that: a badge is a to-do list, and counting cleared failures made
    the System button climb forever while its panel stayed empty."""
    from mediaforge.web import db as DB
    from mediaforge.web.routes import home_panels as R

    # Deltas, not absolutes: the session database is shared with every other
    # test in the run, and a test that empties download_queue to get a clean
    # number decides what the tests after it see.
    before_failed = R._failed_count()
    before_queue = R._queue_badge()
    before_all = (DB.get_queue_stats()["by_status"] or {}).get("failed", 0)

    _queue_row("failed", 0)      # still in the queue
    _queue_row("failed", 1)      # cleared away by the user
    _queue_row("failed", 1)
    _queue_row("queued", 0)

    assert R._failed_count() - before_failed == 1
    assert R._queue_badge() - before_queue == 1
    # The statistics still see every row -- that is why they are kept.
    assert (DB.get_queue_stats()["by_status"] or {}).get("failed", 0) - before_all == 3


# ── storage: which paths count as one disk ───────────────────────────────

@pytest.fixture()
def fresh_disk_cache():
    from mediaforge.web.routes import home_panels as R
    R._disk_cache["at"] = 0.0
    R._disk_cache["value"] = []
    yield R
    R._disk_cache["at"] = 0.0
    R._disk_cache["value"] = []


class _Usage:
    def __init__(self, total, free):
        self.total, self.free = total, free
        self.used = total - free


def _fake_storage(monkeypatch, roots, usage_by_path, dev_by_path):
    from mediaforge.web.routes import home_panels as R
    monkeypatch.setattr(R, "_download_roots", lambda: roots)
    monkeypatch.setattr(R.shutil, "disk_usage",
                        lambda p: _Usage(*usage_by_path[str(p)]))
    monkeypatch.setattr(R, "_device_id", lambda p: dev_by_path.get(str(p)))


def test_docker_bind_mounts_of_one_export_are_one_row_naming_all_of_them(
        fresh_disk_cache, monkeypatch):
    """Six bind mounts of /mnt/nas/... are one filesystem, so they share an
    st_dev. They must collapse to one bar that still names all six -- the old
    code kept only the first label and the other five vanished."""
    R = fresh_disk_cache
    names = ["Downloads", "Anime", "Serien", "XXX", "Filme", "Books"]
    roots = [(n, "/app/" + n) for n in names]
    usage = {"/app/" + n: (7 * 1024 ** 4, 3 * 1024 ** 4) for n in names}
    devs = {"/app/" + n: 2049 for n in names}       # one superblock
    _fake_storage(monkeypatch, roots, usage, devs)

    rows = R._disk_rows()
    assert len(rows) == 1
    for name in names:
        assert name in rows[0][0]


def test_datasets_sharing_a_pool_are_one_row_despite_different_st_dev(
        fresh_disk_cache, monkeypatch):
    """ZFS datasets and btrfs subvolumes get their own st_dev but share the
    pool's free space. Keying on st_dev alone would draw one identical bar
    per dataset and claim they are independent disks."""
    R = fresh_disk_cache
    roots = [("Filme", "/a"), ("Serien", "/b")]
    usage = {"/a": (7 * 1024 ** 4, 3 * 1024 ** 4),
             "/b": (7 * 1024 ** 4, 3 * 1024 ** 4)}
    _fake_storage(monkeypatch, roots, usage, {"/a": 60, "/b": 61})

    rows = R._disk_rows()
    assert len(rows) == 1
    assert "Filme" in rows[0][0] and "Serien" in rows[0][0]


def test_separately_mounted_shares_stay_separate(fresh_disk_cache, monkeypatch):
    """One NAS, but each share mounted on its own on the host: different
    superblocks AND different free space, so they are different rows."""
    R = fresh_disk_cache
    roots = [("Downloads", "/a"), ("Filme NAS", "/b"), ("eBooks", "/c")]
    usage = {"/a": (930 * 1024 ** 3, 155 * 1024 ** 3),
             "/b": (7 * 1024 ** 4, 3 * 1024 ** 4),
             "/c": (3 * 1024 ** 4, 1 * 1024 ** 4)}
    _fake_storage(monkeypatch, roots, usage, {"/a": 60, "/b": 61, "/c": 62})

    rows = R._disk_rows()
    assert [r[0] for r in rows] == ["Downloads", "Filme NAS", "eBooks"]


def test_an_unreadable_path_does_not_take_the_others_down(
        fresh_disk_cache, monkeypatch):
    """A path that is not mounted right now is skipped, not an error."""
    R = fresh_disk_cache
    monkeypatch.setattr(R, "_download_roots",
                        lambda: [("Gone", "/gone"), ("Here", "/here")])

    def _usage(path):
        if str(path) == "/gone":
            raise OSError("not mounted")
        return _Usage(1000, 400)
    monkeypatch.setattr(R.shutil, "disk_usage", _usage)
    monkeypatch.setattr(R, "_device_id", lambda p: 7)

    rows = R._disk_rows()
    assert [r[0] for r in rows] == ["Here"]
