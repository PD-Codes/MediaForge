"""Setting-change events: who hears about a settings write, and how often.

* db.set_setting() notifies its listeners only when the value really changed
  (the settings pages save whole sections, so most writes repeat the stored
  value -- each used to write an audit row and restart module workers).
* The module listener ignores the core's own ``module:<id>:__*`` bookkeeping
  keys and handles a burst of writes for one module in a single dispatch.
"""

import threading
import time

import pytest


@pytest.fixture()
def heard(app):
    """Register a recording setting listener for the duration of a test."""
    from mediaforge.web.db import settings as settings_db

    calls = []

    def _listener(key, value):
        calls.append((key, value))

    settings_db.add_setting_listener(_listener)
    try:
        yield calls
    finally:
        settings_db._SETTING_LISTENERS.remove(_listener)


def test_unchanged_value_notifies_nobody(app, heard):
    from mediaforge.web.db import set_setting

    with app.app_context():
        set_setting("pytest_event_plain", "one")
        set_setting("pytest_event_plain", "one")
        set_setting("pytest_event_plain", "two")
    keys = [k for k, _v in heard if k == "pytest_event_plain"]
    assert keys == ["pytest_event_plain", "pytest_event_plain"]
    assert [v for k, v in heard if k == "pytest_event_plain"] == ["one", "two"]


def test_unchanged_secret_notifies_nobody(app, heard):
    from mediaforge.web.db import delete_setting, get_setting, set_setting

    with app.app_context():
        before = get_setting("seerr_api_key")
        try:
            set_setting("seerr_api_key", "first-secret")
            set_setting("seerr_api_key", "first-secret")
            set_setting("seerr_api_key", "second-secret")
            assert get_setting("seerr_api_key") == "second-secret"
        finally:
            # Shared test DB: a configured Seerr key would change other tests.
            if before is None:
                delete_setting("seerr_api_key")
            else:
                set_setting("seerr_api_key", before)
    assert [v for k, v in heard if k == "seerr_api_key"][:2] == ["first-secret", "second-secret"]


def test_module_listener_ignores_bookkeeping_and_coalesces(monkeypatch):
    from mediaforge.web import thirdparties as tp

    fired = []
    synced = []
    done = threading.Event()

    def _fire(name, hook, app, keys):
        fired.append((name, hook, keys))

    def _sync(app, module_name=None, restart=False):
        synced.append((module_name, restart))
        done.set()

    monkeypatch.setattr(tp, "_APP", object())
    monkeypatch.setattr(tp, "fire_module_hook", _fire)
    monkeypatch.setattr(tp, "sync_workers", _sync)
    monkeypatch.setattr(tp, "_module_name_for_setting_key",
                        lambda key: "demo" if key.startswith("module:demo:") else None)
    monkeypatch.setattr(tp, "_SETTINGS_CHANGE_DEBOUNCE", 0.2)

    tp._on_setting_changed("module:demo:__installed_version", "1.0")
    time.sleep(0.3)
    assert fired == [] and synced == []

    tp._on_setting_changed("module:demo:token", "a")
    tp._on_setting_changed("module:demo:channel", "b")
    tp._on_setting_changed("module:demo:token", "c")
    assert done.wait(5)
    time.sleep(0.1)
    assert fired == [("demo", "on_settings_changed", ("module:demo:token", "module:demo:channel"))]
    assert synced == [("demo", True)]
    assert tp._PENDING_SETTING_KEYS == {}
