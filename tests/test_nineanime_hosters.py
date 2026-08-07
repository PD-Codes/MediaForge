"""9anime labels every server "HD" -- the hoster comes from the embed host.

The site calls each server by QUALITY, not by hoster, while actually embedding
more than one (MegaPlay, and its own player on my.1anime.site). Keying anything
off that label meant:

* the provider dropdown came back empty ("no sources") for episodes that play
  fine, because "HD" is in no WORKING_PROVIDERS list, and
* the name the user picked could never match the key stored on the episode.

Both are pinned here. No network: the server payload is the shape
scraper.fetch_episode_servers() returns, captured from a live response.
"""

import pytest

from mediaforge.extractors import provider_for_url
from mediaforge.models.nineanime_to import episode as ep_mod


LIVE_EMBED = "https://my.1anime.site/play/f40f4e9fae400ec00aad79364c9467d9"
MEGAPLAY_EMBED = "https://megaplay.buzz/stream/mal/12345/6/sub"


def test_the_players_host_is_recognised():
    assert provider_for_url(LIVE_EMBED) == "oneanime"
    assert provider_for_url("https://cdn.1anime.site/play/x") == "oneanime"
    assert provider_for_url(MEGAPLAY_EMBED) == "megaplay"


@pytest.mark.parametrize("embed, label, expected", [
    (LIVE_EMBED, "HD", "OneAnime"),
    (MEGAPLAY_EMBED, "HD", "Megaplay"),
    # Unknown host: keep whatever the site showed rather than inventing a name.
    ("https://some.new.host.invalid/e/x", "HD", "HD"),
])
def test_canonical_hoster(embed, label, expected):
    assert ep_mod._canonical_hoster(embed, label) == expected


def test_provider_list_is_labelled_by_host_not_by_quality():
    """The regression itself: {"HD": <my.1anime.site url>} must not vanish."""
    from mediaforge.web.routes.search import _label_by_host

    assert _label_by_host({"HD": LIVE_EMBED}) == ["OneAnime"]
    assert _label_by_host({"HD": LIVE_EMBED, "HD-2": MEGAPLAY_EMBED}) == \
        ["OneAnime", "Megaplay"]
    # Two labels, one host -> one entry, not a duplicated dropdown row.
    assert _label_by_host({"HD": LIVE_EMBED, "HD-2": LIVE_EMBED}) == ["OneAnime"]
    # Nothing playable at all -> empty, so the UI can say so honestly.
    assert _label_by_host({"HD": "https://unknown.invalid/x"}) == []


def _episode_with(servers):
    """A NineAnimeEpisode whose provider_data is prefilled -- no network."""
    from mediaforge.config import Audio, Subtitles

    ep = ep_mod.NineAnimeEpisode(
        url="https://9anime.or.at/some-show-episode-1-english-subbed/"
    )
    key = (Audio.JAPANESE, Subtitles.ENGLISH)
    ep._NineAnimeEpisode__provider_data = {key: servers}
    return ep


def test_provider_link_falls_back_to_what_the_episode_offers():
    """The default hoster ("Megaplay") is usually NOT the one on offer now, and
    an exact-match-only lookup made that look like "no link"."""
    ep = _episode_with({"OneAnime": LIVE_EMBED})
    assert ep.selected_provider == "Megaplay"      # the historical default
    assert ep.provider_link() == LIVE_EMBED        # ...must still resolve
    assert ep.provider_url == LIVE_EMBED


def test_an_explicitly_requested_hoster_is_not_silently_swapped():
    """Falling back is right for the default; it would be wrong for a caller
    that named one hoster on purpose."""
    ep = _episode_with({"OneAnime": LIVE_EMBED})
    assert ep.provider_link(provider="Megaplay") is None
    assert ep.provider_link(provider="OneAnime") == LIVE_EMBED
    # Spelling differences in case must still match.
    assert ep.provider_link(provider="oneanime") == LIVE_EMBED
