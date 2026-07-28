"""The shared SxxExx reader (web/episode_marker.py).

Every "do I already have this episode?" answer comes from a file name, so the
scanner, the download dialog and auto-sync have to read one the same way. They
did not: three copies of the old S(\\d{2})E(\\d{2,3}) pattern silently truncated
anything longer than three digits, which AniWorld's absolute episode numbering
turns from an exotic case into an everyday one.
"""

import pytest

from mediaforge.web.episode_marker import (
    EPISODE_MARKER_RE,
    FALLBACK_EPISODE_RE,
    season_episode_from_name,
)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("One Piece S01E063.mkv", (1, 63)),
        # The regression: the old pattern read this as episode 112.
        ("One Piece S01E1128.mkv", (1, 1128)),
        # The regression that motivated the library-side fix: episode 13 was
        # indexed as episode 1 and collided with the real S02E001.
        ("Show S02E0013.mkv", (2, 13)),
        ("Show S1E1.mkv", (1, 1)),
        ("Show s03e07 - Title.mp4", (3, 7)),
        ("Show S01E01 - S01E02.mkv", (1, 1)),       # first marker wins
        ("Some Movie (2019).mkv", (None, None)),
        ("Show E013.mkv", (None, None)),            # no season -> no guess
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_season_episode_from_name(name, expected):
    assert season_episode_from_name(name) == expected


def test_marker_does_not_truncate():
    """Whole number or no match — never a prefix of one."""
    assert EPISODE_MARKER_RE.search("S01E1128").group(2) == "1128"


def test_fallback_still_needs_two_digits():
    """A bare "E1" appears inside far too many real titles to be a marker."""
    assert FALLBACK_EPISODE_RE.search("Show E1 Title") is None
    assert FALLBACK_EPISODE_RE.search("Show E013 Title").group(1) == "013"
