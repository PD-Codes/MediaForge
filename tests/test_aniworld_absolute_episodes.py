"""AniWorld's absolute episode numbering (Settings -> Downloads).

AniWorld splits long-running shows into site-side seasons but keeps one
continuous count, which it appends to the episode title as "[Episode 062]".
With `aniworld_absolute_episodes` on, that number -- not the season-relative
one -- is what the file name is built from.

The risk this guards is not the regex, it is the fallbacks: an episode without
a marker, a movie entry, and the setting being off must all keep the old
S02E002 name, because anything else silently renumbers an existing library.
"""

import pytest

from mediaforge.models.aniworld_to.episode import (
    AniworldEpisode,
    parse_absolute_episode_number,
)

EP_URL = "https://aniworld.to/anime/stream/one-piece/staffel-2/episode-2"
FILM_URL = "https://aniworld.to/anime/stream/one-piece/filme/film-1"

MARKED_TITLE = (
    "A Promise Between Men! Luffy and the Whale Vow to Meet Again! [Episode 063]"
)


@pytest.mark.parametrize(
    "title, expected",
    [
        (MARKED_TITLE, 63),
        ("x [Episode 062]", 62),          # leading zeros are not octal
        ("x [episode 7]", 7),             # the site's casing varies
        ("x [ Episode  1234 ]", 1234),    # stray whitespace
        ("Ein Bad in Magensäure [Folge 62]", 62),
        ("Das Versprechen", None),        # the normal case: no marker
        ("Episode 63", None),             # brackets are what makes it a marker
        ("x [Episode 0]", None),          # 0 would break the fallback chain
        ("", None),
        (None, None),
    ],
)
def test_parse_absolute_episode_number(title, expected):
    assert parse_absolute_episode_number(title) == expected


def test_parse_prefers_the_first_title_that_carries_a_marker():
    assert parse_absolute_episode_number(None, "de [Folge 5]", "en [Episode 9]") == 5


def _episode(monkeypatch, enabled, title_en, url=EP_URL):
    monkeypatch.setenv("MEDIAFORGE_ANIWORLD_ABSOLUTE_EPISODES", "1" if enabled else "0")
    return AniworldEpisode(
        url=url, episode_number=2, title_de="Das Versprechen", title_en=title_en
    )


def test_off_by_default_keeps_the_season_relative_number(monkeypatch):
    ep = _episode(monkeypatch, False, MARKED_TITLE)
    assert ep.absolute_episode_number == 63     # the fact is still reported
    assert ep.file_episode_number == 2          # but it is not used


def test_on_uses_the_absolute_number(monkeypatch):
    ep = _episode(monkeypatch, True, MARKED_TITLE)
    assert ep.file_episode_number == 63


def test_on_without_a_marker_falls_back(monkeypatch):
    ep = _episode(monkeypatch, True, "A Town that Welcomes Pirates?")
    assert ep.absolute_episode_number is None
    assert ep.file_episode_number == 2


def test_movies_are_never_renumbered(monkeypatch):
    ep = _episode(monkeypatch, True, MARKED_TITLE, url=FILM_URL)
    assert ep.is_movie
    assert ep.file_episode_number == 2


def test_file_name_follows_the_setting(monkeypatch, tmp_path):
    """The point of the whole feature: what lands on disk."""
    monkeypatch.setenv(
        "MEDIAFORGE_NAMING_TEMPLATE",
        "{title}/Season {season}/{title} S{season}E{episode}.mkv",
    )

    class _Series:
        title_cleaned = "One Piece"
        release_year = "1999"
        imdb = "tt0388629"

    class _Season:
        season_number = 2

    for enabled, expected in ((False, "One Piece S02E002"), (True, "One Piece S02E063")):
        ep = _episode(monkeypatch, enabled, MARKED_TITLE)
        ep._series = _Series()
        ep._season = _Season()
        ep.selected_path = str(tmp_path)
        assert ep._file_name == expected
