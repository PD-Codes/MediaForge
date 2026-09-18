"""Regression guard for issue #31: "Download loops between Download and
Encoding, then fails with 'Conversion failed'".

A failure in a LOCAL step that runs after the stream is on disk -- the ffmpeg
tagging/transcoding pass, the mux, the upscale, the move -- used to travel
through the queue worker's ordinary retry and provider-fallback plan. Every
attempt re-fetched the whole episode and then failed in exactly the same place,
so the UI cycled Download -> Encoding -> Download -> Encoding before finally
surfacing the ffmpeg error.
"""

import re
import sqlite3
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "mediaforge"
COMMON = SRC / "models" / "common" / "common.py"
QUEUE_WORKER = SRC / "web" / "queue_worker.py"
QUEUE_JS = SRC / "web" / "static" / "queue.js"


# --- the exception itself -------------------------------------------------

def test_local_step_marks_failures_as_post_download():
    from mediaforge.models.common.common import PostDownloadError, _local_step

    def _boom():
        raise RuntimeError("ffmpeg error (rc=1): No VA display found")

    try:
        _local_step(_boom)
    except PostDownloadError as exc:
        assert "No VA display found" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("_local_step swallowed the failure")


def test_local_step_lets_cancellations_through():
    from mediaforge.models.common.common import PostDownloadError, _local_step

    def _cancelled():
        raise RuntimeError("Download cancelled")

    try:
        _local_step(_cancelled)
    except PostDownloadError:  # pragma: no cover - would be the bug
        raise AssertionError("a user cancel must not look like a local failure")
    except RuntimeError as exc:
        assert "cancelled" in str(exc)


def test_download_flags_the_point_where_the_streams_are_on_disk():
    src = COMMON.read_text(encoding="utf-8")
    assert "_downloads_done = False" in src
    assert src.count("_downloads_done = True") >= 2, (
        "every download branch has to mark the point after which failures are local"
    )
    assert "raise PostDownloadError(str(_exc)) from _exc" in src


def test_queue_worker_does_not_retry_a_post_download_failure():
    src = QUEUE_WORKER.read_text(encoding="utf-8")
    block = re.search(
        r"if isinstance\(e, PostDownloadError\):(.*?)\n                        if ",
        src,
        re.S,
    )
    assert block, "queue worker no longer special-cases PostDownloadError"
    assert "break" in block.group(1), (
        "a post-download failure must leave the retry/provider loop at once"
    )


# --- the settings database the download path reads ------------------------

def test_encoding_settings_come_from_the_configured_config_dir(tmp_path, monkeypatch):
    """A hardcoded ~/.mediaforge pointed the download path at a different
    database than web/db/_core.py's DB_PATH whenever MEDIAFORGE_CONFIG_DIR was
    set, so the download transcoded with defaults while the encoding worker
    used the user's real settings."""
    from mediaforge.models.common import common as mf_common

    db = tmp_path / "mediaforge.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO app_settings VALUES ('encoding_mode', 'h265')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(mf_common, "MEDIAFORGE_CONFIG_DIR", tmp_path)
    assert mf_common._read_encoding_settings() == {"encoding_mode": "h265"}


# --- crash recovery may only run in the process that owns the workers -----

def test_ensure_queue_worker_is_a_noop_without_worker_ownership(monkeypatch):
    from mediaforge.web import queue_worker as qw
    from mediaforge.web import worker_host

    monkeypatch.setattr(worker_host, "_is_worker_host", False)
    monkeypatch.setenv("MEDIAFORGE_WORKER_MODE", "external")
    monkeypatch.setattr(qw, "_queue_worker_started", False)

    called = []
    monkeypatch.setattr(qw.threading, "Thread", lambda *a, **k: called.append(a))

    qw._ensure_queue_worker()
    assert not called, (
        "a web process in external worker mode must not start its own queue "
        "worker -- it would reset and re-download the worker host's running item"
    )


# --- the phase rail the user actually watches -----------------------------

def test_phase_rail_keeps_the_last_reported_phase():
    src = QUEUE_JS.read_text(encoding="utf-8")
    assert 'fp.phase || (_stickyProgressById[item.id] || {}).phase || "download"' in src, (
        "an empty phase between two ffmpeg passes must not read as a new download"
    )
