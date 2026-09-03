"""An update that never finished must leave a trace, not just a state file.

The incident this covers: a self-update was started, the helper was killed
before it could relaunch, and afterwards nothing explained why — the temp log
is truncated on every start and the state file said "installing" to nobody.
"""

import json

import pytest

from mediaforge.web import selfupdate


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point the update state files at a scratch directory."""
    monkeypatch.setattr(selfupdate, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(selfupdate, "STATE_FILE", tmp_path / "update.state")
    monkeypatch.setattr(selfupdate, "META_FILE", tmp_path / "update.meta.json")
    monkeypatch.setattr(selfupdate, "LOG_FILE", tmp_path / "update.log")
    return tmp_path


def test_supervisor_detects_systemd(monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    assert selfupdate._supervisor() == "systemd"
    monkeypatch.delenv("INVOCATION_ID")
    monkeypatch.setattr(selfupdate, "_in_docker", lambda: False)
    assert selfupdate._supervisor() is None


def test_interrupted_update_is_logged_and_reported(cfg, monkeypatch, caplog):
    (cfg / "update.state").write_text("installing", encoding="utf-8")
    (cfg / "update.meta.json").write_text(
        json.dumps({"from_version": "1.2.3", "supervisor": "systemd"}), encoding="utf-8")

    reported = {}
    monkeypatch.setattr(selfupdate, "_report_self_update_flag", lambda: None)
    monkeypatch.setattr(selfupdate, "_report_self_update",
                        lambda **kw: reported.update(kw))

    with caplog.at_level("ERROR", logger="mediaforge"):
        selfupdate.finalize_after_restart()

    assert (cfg / "update.state").read_text(encoding="utf-8") == "failed"
    meta = json.loads((cfg / "update.meta.json").read_text(encoding="utf-8"))
    # The user-facing reason names the actual cause, not just "did not complete"
    assert "systemd" in meta["error"]
    # Telemetry gets the cause as a classifier...
    assert reported["error_type"] == "interrupted_systemd"
    # ...and the local record exists at ERROR level, which is what mf.err keeps.
    assert any("did not complete" in r.message or "did not complete" in r.getMessage()
               for r in caplog.records)


def test_interrupted_update_reports_plain_when_unsupervised(cfg, monkeypatch):
    (cfg / "update.state").write_text("installing", encoding="utf-8")
    (cfg / "update.meta.json").write_text(json.dumps({"from_version": "1.2.3"}),
                                          encoding="utf-8")
    reported = {}
    monkeypatch.setattr(selfupdate, "_report_self_update_flag", lambda: None)
    monkeypatch.setattr(selfupdate, "_report_self_update",
                        lambda **kw: reported.update(kw))
    selfupdate.finalize_after_restart()
    assert reported["error_type"] == "interrupted"


def test_error_log_survives_a_restart(tmp_path, monkeypatch):
    """mf.err is appended, not truncated — that is its whole reason to exist."""
    import importlib

    monkeypatch.setenv("MEDIAFORGE_CONFIG_DIR", str(tmp_path))
    import mediaforge.logger as mflogger
    for _ in range(2):  # two "app starts"
        importlib.reload(mflogger)
        mflogger.get_logger(__name__).error("boom")
    assert (tmp_path / "mf.err").read_text(encoding="utf-8").count("boom") == 2
