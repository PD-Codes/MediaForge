"""Path validation for the endpoints that read or replace files on disk.

lib_resolve_library_file() is the single gate: /api/upscale/add-library used to
have none at all, so any logged-in user could name an arbitrary path and have
the upscale worker overwrite it (replace-original is on by default).
"""

import os
from pathlib import Path

import pytest


@pytest.fixture()
def library(app, tmp_path, monkeypatch):
    """A download root with one real video file in it."""
    root = tmp_path / "library"
    (root / "Some Show").mkdir(parents=True)
    video = root / "Some Show" / "Some Show - S01E01.mkv"
    video.write_bytes(b"\x00" * 16)
    monkeypatch.setenv("MEDIAFORGE_DOWNLOAD_PATH", str(root))
    with app.app_context():
        from mediaforge.web import db
        db.set_setting("download_path", str(root))
    return {"root": root, "video": video}


def _resolve(app, path):
    from mediaforge.web.routes.library import lib_resolve_library_file
    with app.test_request_context():
        return lib_resolve_library_file(str(path))


def test_accepts_a_file_inside_the_library(app, library):
    assert _resolve(app, library["video"]) is not None


def test_rejects_a_path_outside_the_library(app, library, tmp_path):
    outside = tmp_path / "elsewhere.mkv"
    outside.write_bytes(b"\x00" * 16)
    assert _resolve(app, outside) is None


def test_rejects_traversal(app, library):
    assert _resolve(app, library["root"] / ".." / "elsewhere.mkv") is None


def test_rejects_a_non_video_file(app, library):
    other = library["root"] / "Some Show" / "notes.txt"
    other.write_text("hello")
    assert _resolve(app, other) is None


def test_rejects_a_missing_file(app, library):
    assert _resolve(app, library["root"] / "Some Show" / "nope.mkv") is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_rejects_a_symlink_pointing_out_of_the_library(app, library, tmp_path):
    secret = tmp_path / "secret.mkv"
    secret.write_bytes(b"\x00" * 16)
    link = library["root"] / "Some Show" / "link.mkv"
    link.symlink_to(secret)
    assert _resolve(app, link) is None
