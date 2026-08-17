"""The two fields added for the store's 2026-08-17 server change.

Both are one line of code each, and both fail silently rather than loudly if they break —
a missing key in a dict does not raise, it just makes a button disappear or a statistic
count nothing. That is exactly the kind of thing that needs a check.
"""

from mediaforge.web.thirdparties import store


def _entry(**overrides):
    base = {"id": "pushover-notify", "folder": "pushover_notify", "name": "Pushover",
            "version": "1.0.0", "download_url": "packages/x.mfmod"}
    base.update(overrides)
    return base


def test_repo_url_survives_normalisation():
    """_normalize() is an allowlist — a key it does not name is dropped on the floor."""
    out = store._normalize(_entry(repo_url="https://github.com/PD-Codes/mf-pushover"),
                           "https://store.example/index.json")
    assert out["repo_url"] == "https://github.com/PD-Codes/mf-pushover"


def test_a_missing_repository_is_empty_not_derived():
    """No link means no button. It must never be built out of the "owner/repo" string:
    a store that links something which is not GitHub would get a dead github.com URL."""
    out = store._normalize(_entry(repository="PD-Codes/mf-pushover"),
                           "https://store.example/index.json")
    assert out["repo_url"] == ""


def test_the_applied_theme_is_reported_as_default_when_none_is_set(monkeypatch):
    """The built-in look is reported as "default", never as an empty string or a missing
    key — the store counts "not reported" and "using the default" as different facts."""
    from mediaforge.telemetry import events, settings
    from mediaforge.web import themes

    monkeypatch.setattr(settings, "is_key_enabled", lambda key: key == "system_info")
    monkeypatch.setattr(themes, "active_theme", lambda: None)

    payload = events.build_system_info_event()["payload"]
    assert payload["active_theme"] == "default"


def test_the_applied_theme_is_reported_by_its_store_id(monkeypatch):
    from mediaforge.telemetry import events, settings
    from mediaforge.web import themes

    monkeypatch.setattr(settings, "is_key_enabled", lambda key: key == "system_info")
    monkeypatch.setattr(themes, "active_theme",
                        lambda: {"id": "nord-deep", "folder": "nord_deep"})

    payload = events.build_system_info_event()["payload"]
    # The manifest id, not the folder: the folder is a local name and the store joins
    # its catalog on the id.
    assert payload["active_theme"] == "nord-deep"
