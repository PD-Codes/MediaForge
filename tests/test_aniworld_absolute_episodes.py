"""AniWorld's absolute episode numbering (Settings -> Downloads).

AniWorld splits long-running shows into site-side seasons but keeps one
continuous count, which it appends to the episode title as "[Episode 062]".
With `aniworld_absolute_episodes` on, that number -- not the season-relative
one -- is what the file name is built from, and the season collapses to 1 with
it: the show is one long season in absolute order, which is what TMDB/TVDB and
therefore Jellyfin expect.

The risk this guards is not the regex, it is the fallbacks: an episode without
a marker, a movie entry, and the setting being off must all keep the old
S02E002 name, because anything else silently renumbers an existing library.
And season and episode must always move together -- S01E002 or S02E063 would be
a library that matches nothing.
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
    assert (ep.file_season_number, ep.file_episode_number) == (2, 2)  # not used


def test_on_uses_the_absolute_number_and_season_one(monkeypatch):
    ep = _episode(monkeypatch, True, MARKED_TITLE)
    assert (ep.file_season_number, ep.file_episode_number) == (1, 63)


def test_on_without_a_marker_falls_back(monkeypatch):
    ep = _episode(monkeypatch, True, "A Town that Welcomes Pirates?")
    assert ep.absolute_episode_number is None
    assert (ep.file_season_number, ep.file_episode_number) == (2, 2)


def test_movies_are_never_renumbered(monkeypatch):
    """/filme entries are counted per collection and live in season 0."""
    ep = _episode(monkeypatch, True, MARKED_TITLE, url=FILM_URL)
    assert ep.is_movie
    assert ep.season.season_number == 0
    assert (ep.file_season_number, ep.file_episode_number) == (0, 2)


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

    for enabled, expected in ((False, "One Piece S02E002"), (True, "One Piece S01E063")):
        ep = _episode(monkeypatch, enabled, MARKED_TITLE)
        ep._series = _Series()
        ep._season = _Season()
        ep.selected_path = str(tmp_path)
        assert ep._file_name == expected


# ---------------------------------------------------------------------------
# Presence detection across the switch
# ---------------------------------------------------------------------------
# The setting decides what a NEW file is called. A library built before the
# switch is still on disk under the old name, and "is this episode here?" has
# to answer yes for either -- otherwise flipping the setting reports a complete
# show as entirely missing and downloads all of it a second time.


def test_candidates_cover_both_naming_schemes(monkeypatch):
    for enabled in (False, True):
        ep = _episode(monkeypatch, enabled, MARKED_TITLE)
        assert set(ep.file_number_candidates) == {(2, 2), (1, 63)}, enabled


def test_candidates_start_with_what_a_new_download_gets(monkeypatch):
    """Order matters: the first pair is the name that would be written."""
    assert _episode(monkeypatch, True, MARKED_TITLE).file_number_candidates[0] == (1, 63)
    assert _episode(monkeypatch, False, MARKED_TITLE).file_number_candidates[0] == (2, 2)


def test_candidates_without_a_marker_are_just_the_one_pair(monkeypatch):
    ep = _episode(monkeypatch, True, "A Town that Welcomes Pirates?")
    assert ep.file_number_candidates == ((2, 2),)


def test_an_old_library_is_still_recognised(monkeypatch):
    """The scenario: absolute numbering switched on over an existing library."""
    on_disk = {(2, 2)}                      # downloaded before the switch
    ep = _episode(monkeypatch, True, MARKED_TITLE)
    assert any(pair in on_disk for pair in ep.file_number_candidates)
    # ...and the file a fresh download would write is still the new name.
    assert (ep.file_season_number, ep.file_episode_number) == (1, 63)
