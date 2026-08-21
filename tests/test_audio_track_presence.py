"""The two pieces auto-sync's multi-language mode stands on.

`language_present_in()` decides whether an episode already carries a language,
and the audio_track_cache decides how often that costs an ffprobe. Both are
load-bearing in a quiet way: get the first one wrong and auto-sync either
re-queues a finished series forever or silently drops a track nobody notices
is missing; get the second one wrong and every sync cycle probes the whole
library.
"""

import os
import time

import pytest

from mediaforge.languages import (
    burned_subtitle_labels,
    burns_subtitles,
    language_present_in,
    tracks_for_label,
)


# ── Which tracks a label needs ───────────────────────────────────────────────

def test_dubbed_language_is_decided_by_its_audio_track():
    assert language_present_in({"deu"}, {"und"}, "German Dub")
    assert not language_present_in({"eng"}, {"und"}, "German Dub")


def test_subbed_languages_are_told_apart_by_the_burned_in_subtitles():
    """Both are Japanese audio: the audio code alone cannot decide this.

    "English Sub" and "German Sub" differ only in the subtitles burned into
    the picture, so a file that has one must not count as having the other --
    that is a whole track quietly not downloaded.
    """
    assert tracks_for_label("English Sub")[0] == tracks_for_label("German Sub")[0]

    # Japanese audio with German subtitles burned in.
    assert language_present_in({"jpn"}, {"deu"}, "German Sub")
    assert not language_present_in({"jpn"}, {"deu"}, "English Sub")


def test_missing_video_stream_is_not_present():
    """A file with audio but no video is not a downloaded episode."""
    assert not language_present_in({"deu"}, set(), "German Dub")


def test_unknown_label_reads_as_not_present():
    """A module's own language must be queued, not assumed to be there."""
    assert not language_present_in({"deu", "eng"}, {"und"}, "Klingon Dub")


def test_empty_probe_reads_as_not_present():
    """An unreadable file is a reason to queue, never a reason to skip."""
    assert not language_present_in(set(), set(), "German Dub")


# ── Which languages cost a whole video stream ────────────────────────────────

def test_burned_in_languages_are_the_subbed_ones():
    """What the UI warns about has to match what download() actually fetches.

    A language with burned-in subtitles cannot be added as an audio track --
    `download()` pulls a second VIDEO stream for it, which roughly doubles the
    file. The warning in the picker is driven by this list, so if the two ever
    disagree the user is told the wrong thing about their disk.
    """
    assert burns_subtitles("English Sub")
    assert burns_subtitles("German Sub")
    assert burns_subtitles("English Dub (German Sub)")

    assert not burns_subtitles("German Dub")
    assert not burns_subtitles("English Dub")
    # hanime: one logical language, nothing to switch, so it is not flagged.
    assert not burns_subtitles("Japanese Dub")


def test_burned_in_flag_agrees_with_the_track_mapping():
    """The flag is exactly "needs a subtitle-tagged video stream"."""
    for label in burned_subtitle_labels():
        assert tracks_for_label(label)[1] is not None


def test_an_unknown_language_is_not_flagged():
    """No warning beats a wrong warning for a module's own language."""
    assert not burns_subtitles("Klingon Sub")


# ── The probe cache ──────────────────────────────────────────────────────────

@pytest.fixture()
def media_file(tmp_path):
    p = tmp_path / "Show - S01E01.mkv"
    p.write_bytes(b"not really a video")
    return str(p)


def test_cache_returns_what_was_stored(app, media_file):
    from mediaforge.web.db import get_cached_tracks, set_cached_tracks

    assert get_cached_tracks(media_file) is None  # nothing probed yet
    set_cached_tracks(media_file, {"deu"}, {"und"})

    cached = get_cached_tracks(media_file)
    assert cached == {"audio_langs": {"deu"}, "video_langs": {"und"}}


def test_cache_is_invalidated_when_the_file_changes(app, media_file):
    """Muxing a track in rewrites the file, so the old answer must not stand.

    This is the case the whole cache exists for: the second language is merged
    into the primary's file, and the next sync cycle has to see the new track
    without anyone invalidating anything by hand.
    """
    from mediaforge.web.db import get_cached_tracks, set_cached_tracks

    set_cached_tracks(media_file, {"deu"}, {"und"})
    assert get_cached_tracks(media_file) is not None

    time.sleep(0.01)
    with open(media_file, "ab") as fh:
        fh.write(b"a second audio track went in here")

    assert get_cached_tracks(media_file) is None


def test_cache_ignores_a_file_that_is_gone(app, media_file):
    """Reporting tracks for a deleted file would skip an episode we lost."""
    from mediaforge.web.db import get_cached_tracks, set_cached_tracks

    set_cached_tracks(media_file, {"deu"}, {"und"})
    os.remove(media_file)
    assert get_cached_tracks(media_file) is None
