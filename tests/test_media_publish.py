"""Crash safety of web/media_publish.py.

The point of publish_output() is that a failing copy must never cost the file
it was about to replace (see the module docstring there). That is precisely
the kind of thing that cannot be verified by reading the code, because the
interesting case is the one where the copy blows up half way through.
"""

import pytest


@pytest.fixture()
def publish():
    from mediaforge.web import media_publish

    return media_publish


def test_replaces_the_destination(publish, tmp_path):
    """The new payload ends up at the destination, the scratch file is gone."""
    source = tmp_path / "scratch.mkv"
    source.write_bytes(b"new payload")
    target = tmp_path / "out" / "episode.mkv"
    target.parent.mkdir()
    target.write_bytes(b"old payload")

    publish.publish_output(source, target)

    assert target.read_bytes() == b"new payload"
    assert not source.exists()


def test_creates_missing_directories(publish, tmp_path):
    source = tmp_path / "scratch.mkv"
    source.write_bytes(b"payload")
    target = tmp_path / "a" / "b" / "episode.mkv"

    publish.publish_output(source, target)

    assert target.read_bytes() == b"payload"


def test_a_failed_swap_leaves_the_original_alone(publish, tmp_path, monkeypatch):
    """The whole reason this module exists: no data loss on a failed publish."""
    source = tmp_path / "scratch.mkv"
    source.write_bytes(b"new payload")
    target = tmp_path / "episode.mkv"
    target.write_bytes(b"THE ONLY COPY")

    def boom(*_args, **_kwargs):
        raise OSError("target volume went away")

    monkeypatch.setattr(publish.os, "replace", boom)

    with pytest.raises(OSError):
        publish.publish_output(source, target)

    # Untouched, and no staging file left behind next to it.
    assert target.read_bytes() == b"THE ONLY COPY"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(publish._STAGING_SUFFIX)]
    assert leftovers == []


def test_stale_staging_file_is_overwritten(publish, tmp_path):
    """A staging file from a killed run must not block the next publish."""
    source = tmp_path / "scratch.mkv"
    source.write_bytes(b"new payload")
    target = tmp_path / "episode.mkv"
    target.write_bytes(b"old payload")
    (tmp_path / ("episode.mkv" + publish._STAGING_SUFFIX)).write_bytes(b"junk from a dead run")

    publish.publish_output(source, target)

    assert target.read_bytes() == b"new payload"


def test_sweep_removes_only_matching_leftovers(publish, monkeypatch, tmp_path):
    temp_dir = tmp_path / "mediaforge-temp"
    temp_dir.mkdir()
    stale = temp_dir / "something.upscale.mkv"
    keep = temp_dir / "unrelated.txt"
    stale.write_bytes(b"x")
    keep.write_bytes(b"x")
    monkeypatch.setattr(publish, "MEDIAFORGE_TEMP_DIR", temp_dir)

    publish.sweep_stale_temp_files(".upscale.mkv")

    assert not stale.exists()
    assert keep.exists()


def test_sweep_survives_a_missing_temp_dir(publish, monkeypatch, tmp_path):
    """Called on every worker start, so it must never raise."""
    monkeypatch.setattr(publish, "MEDIAFORGE_TEMP_DIR", tmp_path / "does-not-exist")
    publish.sweep_stale_temp_files(".upscale.mkv")


def test_publish_is_used_by_both_workers():
    """Guards the reason this module exists: nobody may go back to unlink+move."""
    from mediaforge.web import encoding_worker, upscale_worker

    for module in (encoding_worker, upscale_worker):
        assert hasattr(module, "publish_output"), module.__name__
