"""Notification wording: DE/EN by UI language, and never "episodes" for a movie.

The bodies used to be hardcoded German, and a movie job / a single-episode job
were both announced with series wording ("Episode(n)", "Neue Folgen").
"""

from mediaforge.web import notifications as n


def test_tr_picks_german_only_for_de():
    assert n.tr("de", "Fehler", "Errors") == "Fehler"
    assert n.tr("en", "Fehler", "Errors") == "Errors"
    # Unknown/missing language falls back to English, like get_locale().
    assert n.tr(None, "Fehler", "Errors") == "Errors"
    assert n.tr("fr", "Fehler", "Errors") == "Errors"


def test_a_movie_is_never_called_an_episode():
    for lang in ("de", "en"):
        text = n.media_count_text(lang, 1, is_movie=True)
        assert "pisode" not in text and "Folge" not in text


def test_single_episode_is_singular():
    assert n.media_count_text("de", 1, False) == "1 Episode"
    assert n.media_count_text("en", 1, False) == "1 episode"
    assert n.media_count_text("de", 4, False) == "4 Episoden"
    assert n.media_count_text("en", 4, False) == "4 episodes"


def test_error_count_is_singular_for_one():
    assert n.error_count_text("en", 1) == "1 error"
    assert n.error_count_text("en", 3) == "3 errors"
    assert n.error_count_text("de", 1) == "1 Fehler"


def test_discord_movie_embed_has_no_episode_count(monkeypatch):
    """A movie is one file: the embed names it instead of counting episodes."""
    sent = {}

    monkeypatch.setattr(n, "_get_setting", lambda key, default="": {
        "notif_discord_webhook_url": "https://example.invalid/hook",
    }.get(key, default or "1"))
    monkeypatch.setattr(n, "_post_json", lambda url, payload, headers=None: sent.update(payload) or 204)
    # Send inline instead of on a daemon thread so the assertions can see it.
    monkeypatch.setattr(n.threading, "Thread",
                        lambda target=None, daemon=None: type("T", (), {"start": lambda s: target()})())

    n.notify_discord("Some Movie", "completed", episode_count=1, errors=[],
                     is_movie=True, lang="en")
    names = [f["name"] for f in sent["embeds"][0]["fields"]]
    values = [f["value"] for f in sent["embeds"][0]["fields"]]
    assert "Episodes" not in names and "Episoden" not in names
    assert "Movie" in values


if __name__ == "__main__":  # pragma: no cover - manual self-check
    test_tr_picks_german_only_for_de()
    test_a_movie_is_never_called_an_episode()
    test_single_episode_is_singular()
    test_error_count_is_singular_for_one()
    print("ok")
