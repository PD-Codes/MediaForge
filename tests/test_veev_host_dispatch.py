"""VeeV is dispatched by resolved host, and module archives reject backslashes.

Two core changes guarded here:

1. ``models.common.common._download_via_hoster()`` -- VeeV used to be branched
   on ``selected_provider == "VEEV"`` inside FilmPalast's and MegaKino's own
   ``download()``. Every other model family (and every third-party module)
   therefore fed a VeeV link into the yt-dlp/ffmpeg pipeline, which its CDN
   rejects, and a mirrored label pointing at veev.to was missed even on the
   sites that had the branch. Dispatch now happens once, on the host.

2. ``web.thirdparties.store._safe_extract()`` -- a member name containing a
   backslash was normalised to "/" for the containment checks but extracted
   verbatim, so ``mod\\..\\..\\db.py`` passed as a nested path and landed as a
   literal file in the staging root.
"""

import io
import sys
import types
import zipfile
from pathlib import Path

import pytest

from mediaforge.models.common.common import _download_via_hoster
from mediaforge.web.thirdparties import store


class _FakeEpisode:
    """The attributes _download_via_hoster() touches -- nothing else."""

    def __init__(self, provider_url, selected_provider, folder):
        self.provider_url = provider_url
        self.selected_provider = selected_provider
        self._file_name = "Test Movie (2026)"
        self._folder_path = folder
        self._episode_path = folder / "Test Movie (2026).mkv"


@pytest.fixture()
def veev_calls(monkeypatch):
    """Stub the VeeV extractor so no browser/CDN is ever touched."""
    calls = []
    stub = types.ModuleType("mediaforge.extractors.provider.veev")
    stub.download_from_veev = lambda url, out, **kw: calls.append((url, out, kw))
    monkeypatch.setitem(sys.modules, "mediaforge.extractors.provider.veev", stub)
    return calls


def test_mislabeled_veev_link_is_still_routed_to_veev(tmp_path, veev_calls):
    """Label says VOE, host says veev.to -- the host wins."""
    ep = _FakeEpisode("https://veev.to/e/abc123", "VOE", tmp_path)
    assert _download_via_hoster(ep) is True
    assert veev_calls and veev_calls[0][0] == "https://veev.to/e/abc123"


def test_label_suffixes_do_not_break_the_match(tmp_path, veev_calls):
    """FilmPalast spells it "VeeV HD"; with an unknown host only the label is
    left, so the suffix must not defeat the comparison."""
    ep = _FakeEpisode("https://mirror.invalid/e/abc", "VeeV HD", tmp_path)
    assert _download_via_hoster(ep) is True
    assert len(veev_calls) == 1


def test_other_hosters_fall_through_to_the_shared_pipeline(tmp_path, veev_calls):
    ep = _FakeEpisode("https://voe.sx/e/abc123", "VOE", tmp_path)
    assert _download_via_hoster(ep) is False
    assert veev_calls == []


def _zip(names, folder="mymod"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{folder}/__init__.py", "")
        for name in names:
            zf.writestr(name, "pwned")
    return buf.getvalue()


def test_backslash_member_is_rejected(tmp_path):
    data = _zip(["mymod\\..\\..\\db.py"])
    with pytest.raises(ValueError, match="backslash"):
        store._safe_extract(data, "mymod", tmp_path)
    assert not any(p.name.endswith("db.py") for p in tmp_path.rglob("*"))


def test_plain_module_still_extracts(tmp_path):
    data = _zip(["mymod/thing.py"])
    staged = store._safe_extract(data, "mymod", tmp_path)
    assert (staged / "thing.py").read_text() == "pwned"
    assert staged == Path(tmp_path) / "mymod"
