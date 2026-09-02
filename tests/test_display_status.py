"""Display detection: a set DISPLAY must not be trusted on its own.

The bug this guards: on a headless Debian a leftover ``DISPLAY`` (systemd unit,
dropped ``ssh -X``) made MediaForge skip the Xvfb fallback and launch Chromium
against a dead X server, which dies with an unreadable TargetClosedError.
"""

import os
import socket
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from mediaforge import autodeps


@pytest.fixture(autouse=True)
def _no_display_cache():
    """display_status() memoizes for 15 s — start every test from cold."""
    autodeps._display_cache = None
    yield
    autodeps._display_cache = None


def test_unset_or_malformed_display_is_not_reachable():
    assert autodeps._x_display_reachable("") is False
    assert autodeps._x_display_reachable("nonsense") is False
    assert autodeps._x_display_reachable(":abc") is False


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="POSIX only")
def test_local_display_reachable_only_while_something_listens(monkeypatch):
    """A real listening socket reads as reachable, a stale path does not."""
    # Short dir on purpose: an AF_UNIX path is capped at ~108 bytes, and
    # pytest's tmp_path already eats most of that.
    sock_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    # _x_display_reachable looks under /tmp/.X11-unix, so fake the number by
    # pointing at a socket we control via a patched connect target.
    path = sock_dir / "X77"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    real_connect = socket.socket.connect

    def fake_connect(self, addr):
        if addr == "/tmp/.X11-unix/X77":
            addr = str(path)
        return real_connect(self, addr)

    monkeypatch.setattr(socket.socket, "connect", fake_connect)
    try:
        assert autodeps._x_display_reachable(":77") is True
    finally:
        srv.close()
        path.unlink(missing_ok=True)
    assert autodeps._x_display_reachable(":77") is False


def test_display_status_reports_unavailable_without_xvfb(monkeypatch):
    monkeypatch.setattr(autodeps, "PLATFORM", "Linux")
    monkeypatch.setattr(autodeps, "_xvfb_proc", None)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(autodeps, "_x_display_reachable", lambda d: False)
    monkeypatch.setattr(autodeps.shutil, "which", lambda name: None)
    assert autodeps.display_status()["mode"] == "unavailable"


def test_display_status_reports_virtual_when_xvfb_present_but_idle(monkeypatch):
    monkeypatch.setattr(autodeps, "PLATFORM", "Linux")
    monkeypatch.setattr(autodeps, "_xvfb_proc", None)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(autodeps.shutil, "which", lambda name: "/usr/bin/Xvfb")
    status = autodeps.display_status()
    assert status["mode"] == "virtual" and status["active"] is False


def test_display_status_native_on_reachable_display(monkeypatch):
    monkeypatch.setattr(autodeps, "PLATFORM", "Linux")
    monkeypatch.setattr(autodeps, "_xvfb_proc", None)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(autodeps, "_x_display_reachable", lambda d: True)
    monkeypatch.setattr(autodeps, "_in_docker", lambda: False)
    assert autodeps.display_status()["mode"] == "native"


def test_display_status_calls_nothing_that_starts_a_display(monkeypatch):
    """Settings and the home page call this on every load — it must be inert."""
    monkeypatch.setattr(autodeps, "PLATFORM", "Linux")
    monkeypatch.setattr(autodeps.subprocess, "Popen",
                        mock.Mock(side_effect=AssertionError("must not spawn")))
    monkeypatch.setattr(autodeps.subprocess, "run",
                        mock.Mock(side_effect=AssertionError("must not install")))
    autodeps.display_status()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
