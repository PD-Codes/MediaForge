"""Moving a finished download over an existing library file (track merge,
quality upgrade) across devices.

The chunked copy used to write into the destination directly: opening it
truncated the user's existing episode, and a failed or cancelled copy then
deleted it. The copy now goes into a part file that is swapped in at the end.
"""

import errno
import threading

import pytest

from mediaforge.models.common import common


@pytest.fixture()
def cross_device(monkeypatch):
    """os.replace(src, dst) fails like a move across devices; part -> dst works."""
    real = common.os.replace

    def fake_replace(a, b):
        if not str(a).endswith(".mfpart"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(a, b)

    monkeypatch.setattr(common.os, "replace", fake_replace)


def _files(tmp_path):
    src, dst = tmp_path / "new.mkv", tmp_path / "Episode.mkv"
    src.write_bytes(b"N" * 1000)
    dst.write_bytes(b"OLD-EPISODE")
    return src, dst


def test_failed_copy_keeps_the_existing_file(tmp_path, cross_device):
    src, dst = _files(tmp_path)

    class Broken:
        def is_set(self):
            raise OSError("NAS disconnected")

    with pytest.raises(OSError):
        common._move_with_progress(src, dst, cancel_event=Broken())
    assert dst.read_bytes() == b"OLD-EPISODE"
    assert not (tmp_path / "Episode.mkv.mfpart").exists()


def test_cancel_keeps_the_existing_file(tmp_path, cross_device):
    src, dst = _files(tmp_path)
    ev = threading.Event()
    ev.set()
    with pytest.raises(RuntimeError, match="Download cancelled"):
        common._move_with_progress(src, dst, cancel_event=ev)
    assert dst.read_bytes() == b"OLD-EPISODE"
    assert not src.exists()
    assert not (tmp_path / "Episode.mkv.mfpart").exists()


def test_copy_replaces_the_file_at_the_end(tmp_path, cross_device):
    src, dst = _files(tmp_path)
    common._move_with_progress(src, dst)
    assert dst.read_bytes() == b"N" * 1000
    assert not src.exists()


def test_same_device_move_overwrites_in_place(tmp_path):
    src, dst = _files(tmp_path)
    common._move_with_progress(src, dst)
    assert dst.read_bytes() == b"N" * 1000
