"""The per-episode retry/fallback plan (web/queue_worker.py).

_build_attempt_plan decides how often each hoster is tried before an episode
counts as failed. Getting it wrong is expensive in both directions: too few
attempts fails downloads that a second try would have completed, too many
turns one dead source into a wall of identical errors.
"""

import pytest


@pytest.fixture()
def build_plan(app):
    """The app fixture initialises the DB the provider order is read from."""
    from mediaforge.web.queue_worker import _build_attempt_plan

    return _build_attempt_plan


def _providers(plan):
    """Provider names in plan order, without the repeated retries."""
    return list(dict.fromkeys(name for name, _attempt, _total in plan))


@pytest.mark.parametrize("provider", ["Direct", "hanime"])
def test_single_source_providers_get_no_fallback_chain(build_plan, provider, app):
    """A source that serves its own stream has no other hoster to fall back to.

    hanime is its own player, and a direct link is a direct link. Walking the
    hoster chain for them means every "other provider" requests the exact same
    URL, which is how one failure used to produce seven identical errors and a
    summary claiming seven sources had been tried.
    """
    with app.app_context():
        plan = build_plan(provider, 3)

    assert _providers(plan) == [provider]
    assert len(plan) == 3
    assert [attempt for _name, attempt, _total in plan] == [1, 2, 3]


def test_a_hoster_keeps_its_retries_then_hands_over(build_plan, app):
    """The picked hoster gets the full retry budget, the rest one shot each."""
    with app.app_context():
        plan = build_plan("VOE", 3)

    assert plan[0][0] == "VOE"
    voe_attempts = [entry for entry in plan if entry[0] == "VOE"]
    assert len(voe_attempts) == 3

    # Every fallback appears exactly once, and none of them is the primary.
    fallbacks = [name for name, _a, _t in plan[3:]]
    assert len(fallbacks) == len(set(fallbacks))
    assert "VOE" not in fallbacks


def test_the_plan_is_never_empty(build_plan, app):
    """The queue worker iterates the plan; an empty one would skip the episode."""
    with app.app_context():
        for provider in ("VOE", "Direct", "hanime", "", None):
            assert build_plan(provider, 1), f"empty plan for {provider!r}"
