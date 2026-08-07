"""The headers a hoster needs must survive the host->provider translation.

`_effective_provider()` picks the hoster from the RESOLVED stream host, which
is right (a site's label often points at a different hoster's domain). But
`extractors.provider_for_url()` answers in the extractor namespace ("voe",
"oneanime") while `PROVIDER_HEADERS_D`/`_W` are keyed by the display name
("VOE", "OneAnime") -- so every host it successfully recognised produced an
EMPTY header set, and the hosters that require a Referer (VeeV, MegaPlay,
EchoVideo, OneAnime) were fetched without one and answered 403.

The bug is invisible for hosters that do not check a Referer, and it hid
behind the fallback path: an UNrecognised host falls through to
`selected_provider`, which is already spelled correctly -- so headers worked
only where the host lookup failed.
"""

import pytest

from mediaforge import config
from mediaforge.extractors import canonical_provider_name, provider_for_url
from mediaforge.models.common.common import _effective_provider


class _Episode:
    def __init__(self, provider_url, selected_provider):
        self.provider_url = provider_url
        self.selected_provider = selected_provider


@pytest.mark.parametrize("key, expected", [
    ("voe", "VOE"),
    ("oneanime", "OneAnime"),
    ("megaplay", "Megaplay"),
    ("echovideo", "EchoVideo"),
    ("VOE", "VOE"),            # already canonical
    ("nosuchhoster", "nosuchhoster"),
    ("", ""),
    (None, None),
])
def test_canonical_provider_name(key, expected):
    assert canonical_provider_name(key) == expected


@pytest.mark.parametrize("stream_url, label, expected", [
    ("https://my.1anime.site/stream/abc", "HD", "OneAnime"),
    ("https://megaplay.buzz/stream/mal/1/1/sub", "HD", "Megaplay"),
    ("https://voe.sx/e/abc", "Vidara", "VOE"),
    # Unknown host -> keep the site's own label, which is already canonical.
    ("https://brand.new.host.invalid/e/x", "VOE", "VOE"),
])
def test_effective_provider_is_the_display_name(stream_url, label, expected):
    assert _effective_provider(_Episode(stream_url, label)) == expected


@pytest.mark.parametrize("stream_url", [
    "https://my.1anime.site/stream/abc",
    "https://megaplay.buzz/stream/mal/1/1/sub",
])
def test_referer_actually_reaches_the_download(stream_url):
    """The point of all of the above: a non-empty header set with a Referer.

    These two hosts answer 403 without one, so an empty dict here is not a
    cosmetic problem -- it is the download failing.
    """
    provider = _effective_provider(_Episode(stream_url, "HD"))
    for headers in (config.PROVIDER_HEADERS_D, config.PROVIDER_HEADERS_W):
        got = headers.get(provider, {})
        assert got, f"no headers for {provider!r} in {headers is config.PROVIDER_HEADERS_D and 'D' or 'W'}"
        assert got.get("Referer"), f"no Referer for {provider!r}"


def test_every_supported_provider_resolves_to_itself():
    """Guards the two lists against drifting apart: a hoster added to
    SUPPORTED_PROVIDERS with a HOST_PROVIDER_MAP entry must round-trip."""
    from mediaforge.extractors import HOST_PROVIDER_MAP

    for host, key in HOST_PROVIDER_MAP.items():
        canonical = canonical_provider_name(key)
        assert canonical in config.SUPPORTED_PROVIDERS or canonical == key, (
            f"{host} maps to {key!r}, which is not a known provider name"
        )
        assert provider_for_url(f"https://{host}/x") == key
