"""18+ telemetry hard rule: the guard must match every spelling of the source.

The queue rows of the 18+ source carry provider "hanime", while the guard only
matched "hanime_tv" -- so a user with stage 4 enabled sent the title and the
error text of those downloads (downloads.titles / downloads.errors).
"""

import pytest

from mediaforge.telemetry import events, sanitize


@pytest.mark.parametrize("spelling", ["hanime_tv", "hanime", "Hanime", "hanime.tv", " HANIME-TV "])
def test_every_spelling_is_adult(spelling):
    assert sanitize.is_adult_provider(spelling)


@pytest.mark.parametrize("other", ["VOE", "aniworld_to", "", None, "Direct"])
def test_other_providers_are_not(other):
    assert not sanitize.is_adult_provider(other)


def test_no_download_event_for_the_queue_spelling(monkeypatch):
    monkeypatch.setattr(events.settings, "is_key_enabled", lambda key: True)
    for status, err in (("completed", None), ("failed", "HTTP 500 from host")):
        assert events.build_download_event(
            provider="hanime", media_type="episode", title="Some Title",
            episode=1, status=status, error_message=err, source="hanime") == []
    # Sanity: the same call for a normal provider does build events.
    assert events.build_download_event(
        provider="VOE", media_type="episode", title="Some Title", episode=1,
        status="failed", error_message="HTTP 500 from host", source="aniworld_to")
