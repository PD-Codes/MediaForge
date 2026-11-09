"""Gap payloads used by the home page's download dialog."""

from mediaforge.web.routes.browse import _feed_gap_rows


def test_gap_dialog_receives_more_than_twelve_slots(monkeypatch):
    monkeypatch.setattr("mediaforge.web.db.get_media_ignores", lambda: {})
    rows = _feed_gap_rows([{
        "folder": "Example", "seasons": {"1": [{"episode": 20}]},
    }])
    assert len(rows[0]["missing"]) == 12
    assert rows[0]["missing_slots"] == [f"S1E{n}" for n in range(1, 20)]
    assert rows[0]["missing_count"] == 19


def test_ignored_episode_in_missing_season_is_sent_to_dialog(monkeypatch):
    monkeypatch.setattr("mediaforge.web.db.get_media_ignores", lambda: {
        "example": {"slots": ["S1E2"]},
    })
    rows = _feed_gap_rows([{
        "folder": "Example", "seasons": {"2": [{"episode": 1}]},
    }])
    assert rows[0]["missing_slots"] == ["S1"]
    assert rows[0]["ignored_slots"] == ["S1E2"]


def test_ignored_series_is_not_offered_for_gap_filling(monkeypatch):
    monkeypatch.setattr("mediaforge.web.db.get_media_ignores", lambda: {
        "example": {"slots": ["__all__"]},
    })
    assert not _feed_gap_rows([{
        "folder": "Example", "seasons": {"1": [{"episode": 3}]},
    }])


def test_statistics_preserve_ignored_slots_for_missing_season_download(monkeypatch):
    from mediaforge.web.routes import stats

    monkeypatch.setattr(stats, "_active_library_cache", lambda cache: cache)
    monkeypatch.setattr(stats, "get_media_ignores", lambda: {"example": {"slots": ["S1E2"]}})
    result = stats._compute_media_stats({"test": {"data": {"titles": [{
        "folder": "Example", "seasons": {"2": [{"episode": 1, "path": "test.mkv"}]},
    }]}}})
    assert result["incomplete"][0]["missing"] == ["S1"]
    assert result["incomplete"][0]["ignored_slots"] == ["S1E2"]
