"""Which Dev Info "release" posts still deserve a banner on the home page.

A release banner exists to tell somebody a new version is out. Told to somebody
who already runs it, it is noise -- and noise they cannot get rid of, because
the banner is rebuilt from the cached feed on every visit and dismissal is only
remembered per browser.

The rule lives in web/version_info.py so that the banner and the update badge
cannot drift apart, and the direction of its uncertainty is deliberate: when
the comparison cannot be made, the banner is SHOWN. Hiding news that mattered
is the worse mistake of the two.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediaforge.web import version_info  # noqa: E402


@pytest.fixture()
def install(monkeypatch):
    """Pretend this instance was installed in a particular way."""

    def configure(version, dev=False, source=False):
        monkeypatch.setattr(version_info, "_get_version", lambda: version)
        monkeypatch.setattr(version_info, "_get_dev_install_info",
                            lambda: (dev, "abc1234def" if dev else None))
        monkeypatch.setattr(version_info, "_is_source_build", lambda: source)

    return configure


def test_the_announced_version_is_the_one_already_installed(install):
    install("2.4.0")
    assert version_info.is_release_already_installed("v2.4.0")
    # The tag is written by hand on the devInfo server, so both spellings have
    # to mean the same thing.
    assert version_info.is_release_already_installed("2.4.0")


def test_a_release_older_than_what_is_installed_is_not_news_either(install):
    install("2.5.1")
    assert version_info.is_release_already_installed("v2.4.0")


def test_a_newer_release_still_gets_its_banner(install):
    install("2.3.9")
    assert not version_info.is_release_already_installed("v2.4.0")


def test_versions_are_compared_as_versions_not_as_strings(install):
    """The one a lexical compare gets wrong: "2.10.0" sorts below "2.4.0"."""
    install("2.4.0")
    assert not version_info.is_release_already_installed("v2.10.0")


def test_a_release_candidate_ranks_below_the_final_release(install):
    install("2.4.0")
    assert version_info.is_release_already_installed("v2.4.0-rc1")


def test_a_dev_branch_install_never_gets_a_release_banner(install):
    """Its version number tracks a moving branch and says nothing about a tag,
    so no comparison against one is meaningful."""
    install("2.3.0", dev=True)
    assert version_info.is_release_already_installed("v2.4.0")


def test_a_local_source_build_never_gets_one_either(install):
    install("2.3.0", source=True)
    assert version_info.is_release_already_installed("v2.4.0")


def test_without_version_metadata_the_banner_is_shown(install):
    """A frozen build may have no package metadata at all. Showing a banner
    that could have been hidden beats hiding one that mattered."""
    install("")
    assert not version_info.is_release_already_installed("v2.4.0")


def test_a_post_without_a_tag_is_not_a_release_post(install):
    install("2.4.0")
    assert not version_info.is_release_already_installed("")
    assert not version_info.is_release_already_installed(None)


def test_a_tag_that_is_not_a_version_falls_back_to_an_exact_match(install):
    install("2.4.0")
    assert not version_info.is_release_already_installed("nightly")
    install("nightly")
    assert version_info.is_release_already_installed("nightly")
