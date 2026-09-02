"""system_info reports the theme packs this install actually wears.

The field used to carry only the admin's instance default, so an install
where every account had picked its own theme still reported "default". It is
now the deduplicated set of the instance default plus every per-account
override, comma-separated like ui_language.
"""

import sqlite3

import pytest

from mediaforge.telemetry import events


@pytest.fixture
def db(monkeypatch):
    """An in-memory DB with just the two tables build_system_info_event reads."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE users (language TEXT)")
    conn.execute("CREATE TABLE user_ui_prefs (user_id INT, key TEXT, value TEXT)")
    conn.commit()
    conn.close_real, conn.close = conn.close, lambda: None  # builder closes per read
    monkeypatch.setattr("mediaforge.web.db.get_db", lambda: conn)
    return conn


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(events.settings, "is_key_enabled", lambda key: True)
    from mediaforge.telemetry import sysinfo
    monkeypatch.setattr(sysinfo, "collect", lambda force=False: {})


def _fake_themes(monkeypatch, default_folder, installed):
    """Stub web.themes: *installed* is a {folder: id} mapping."""
    from mediaforge.web import themes

    monkeypatch.setattr(themes, "installed_themes", lambda refresh=False: [
        {"folder": folder, "id": theme_id, "valid": True}
        for folder, theme_id in installed.items()
    ])
    monkeypatch.setattr(themes, "active_theme", lambda: (
        None if not default_folder
        else {"folder": default_folder, "id": installed[default_folder]}
    ))


def _theme_field(monkeypatch, default_folder, installed, overrides, db):
    for i, value in enumerate(overrides):
        db.execute(
            "INSERT INTO user_ui_prefs VALUES (?, 'theme_pack', ?)", (i, value)
        )
    db.commit()
    _fake_themes(monkeypatch, default_folder, installed)
    return events.build_system_info_event()["payload"]["active_theme"]


def test_default_only(monkeypatch, db):
    assert _theme_field(monkeypatch, "", {}, [], db) == "default"


def test_account_override_is_counted_next_to_the_default(monkeypatch, db):
    field = _theme_field(
        monkeypatch, "", {"lcars_pack": "lcars"}, ["lcars_pack"], db
    )
    assert field == "default,lcars"


def test_empty_override_follows_the_default_and_is_not_double_counted(monkeypatch, db):
    field = _theme_field(
        monkeypatch, "sakura_dir", {"sakura_dir": "sakura"}, ["", "sakura_dir"], db
    )
    assert field == "sakura"


def test_stale_folder_is_dropped_not_reported_raw(monkeypatch, db):
    field = _theme_field(
        monkeypatch, "", {"lcars_pack": "lcars"}, ["uninstalled_pack"], db
    )
    assert field == "default"


def test_explicit_builtin_override_maps_to_default(monkeypatch, db):
    field = _theme_field(
        monkeypatch, "sakura_dir", {"sakura_dir": "sakura"}, ["default"], db
    )
    assert field == "default,sakura"
