"""SerienstreamEpisode.provider_url must only skip the captcha modal once the
redirect has actually left s.to -- not when the mirror failover merely answered
from another s.to mirror, whose page still carries the modal and no provider.
"""

from types import SimpleNamespace

import pytest

from mediaforge.models.s_to import episode as episode_module
from mediaforge.playwright import captcha

EPISODE = "https://serienstream.to/serie/dragonball-z/staffel-7/episode-19"
PROVIDER = "https://voe.sx/e/abc123"


@pytest.fixture
def resolve(monkeypatch):
    """provider_url for a redirect built on *redirect_host* that the session
    answered from *final_url*; returns (provider_url, solver was called)."""
    def run(redirect_host, final_url):
        solved = []
        monkeypatch.setattr(episode_module, "GLOBAL_SESSION",
                            SimpleNamespace(get=lambda url, **kw: SimpleNamespace(url=final_url)))
        monkeypatch.setattr(captcha, "solve_sto_modal", lambda *a, **kw: solved.append(a) or PROVIDER)
        ep = episode_module.SerienstreamEpisode(url=EPISODE, selected_language="German Dub",
                                                selected_provider="VOE")
        ep._SerienstreamEpisode__redirect_url = f"https://{redirect_host}/r?t=token"
        return ep.provider_url, bool(solved)
    return run


def test_a_redirect_that_reached_the_provider_is_taken_as_is(resolve):
    assert resolve("serienstream.to", PROVIDER) == (PROVIDER, False)


def test_the_bare_ip_mirror_is_still_sto_and_gets_the_modal_solved(resolve):
    assert resolve("serienstream.to", "https://186.2.175.5/r?t=token") == (PROVIDER, True)


def test_a_dead_primary_failing_over_to_serienstream_is_still_sto(resolve):
    assert resolve("s.to", "https://serienstream.to/r?t=token") == (PROVIDER, True)


def test_staying_on_the_same_mirror_still_gets_the_modal_solved(resolve):
    assert resolve("serienstream.to", "https://serienstream.to/r?t=token") == (PROVIDER, True)
