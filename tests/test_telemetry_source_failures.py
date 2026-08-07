"""A source site having a bad day is not a MediaForge crash.

Every site model raises its own ``<Site>Unavailable`` when the page it got back
was not usable -- a 404, a challenge interstitial, a maintenance page. Those
reach an ERROR log (routes/search.py logs the provider/season fetch failures
with exc_info), and hooks._TelemetryLogHandler turns ERROR records into
stage-1 crash reports. Without classification, every flaky moment on
filmo.to / 9anime / aniwaves.ru would be filed as a defect in this app --
the same noise the yt-dlp error handler was already changed to avoid.
"""

import pytest

from mediaforge.telemetry.classify import is_transport_failure, is_user_cancellation


@pytest.mark.parametrize("module_path, exc_name, message", [
    ("mediaforge.models.filmo_to.scraper", "FilmoUnavailable", "Movie not found (HTTP 404): x"),
    ("mediaforge.models.filmo_to.scraper", "FilmoTokenExpired", "rejected the CSRF token"),
    ("mediaforge.models.nineanime_to.scraper", "NineAnimeUnavailable", "Not found (HTTP 404): /x"),
    ("mediaforge.models.aniwaves_ru.scraper", "AniwavesUnavailable", "Series not found"),
    ("mediaforge.models.megakino_to.scraper", "MegakinoUnavailable", "challenge page"),
])
def test_site_unavailable_is_kept_out_of_the_crash_channel(module_path, exc_name, message):
    import importlib

    mod = importlib.import_module(module_path)
    exc_type = getattr(mod, exc_name)
    assert is_transport_failure(exc_type, exc_type(message)) is True


def test_a_real_defect_is_still_reported():
    """The filter must stay narrow: a scraper whose regex stopped matching is
    exactly the kind of breakage the crash channel exists for."""
    assert is_transport_failure(ValueError, ValueError("No video source found")) is False
    assert is_transport_failure(KeyError, KeyError("provider_data")) is False
    assert is_transport_failure(AttributeError, AttributeError("'NoneType' has no 'group'")) is False


def test_user_cancellation_is_still_not_a_crash():
    """Unchanged, but pinned next to the above: the project's rule is that a
    user aborting their own download never produces a report."""
    assert is_user_cancellation(message="Download cancelled") is True
