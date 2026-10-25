"""OpenSubtitles title search: AniWorld/s.to episodes carry series and season
MODELS, and the search sent str() of the series object and no season."""

from mediaforge.models.common.common import _subtitle_search_meta


class _Series:
    title = "Frieren"
    imdb = "tt22248376"


class _Season:
    season_number = 2


class _Episode:
    series = _Series()
    season = _Season()
    episode_number = 3
    title_en = "Episode title"


class _Movie:
    title = "Some Movie"
    imdb_id = "tt0000001"


def test_models_are_unwrapped():
    meta = _subtitle_search_meta(_Episode())
    assert meta == {"query": "Frieren", "season": 2, "episode": 3,
                    "imdb_id": "tt22248376", "tmdb_id": None}


def test_plain_attributes_still_work():
    meta = _subtitle_search_meta(_Movie())
    assert meta["query"] == "Some Movie"
    assert meta["imdb_id"] == "tt0000001"
    assert meta["season"] is None
