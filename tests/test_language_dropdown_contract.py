"""The language dropdown must not be built from another site's language set.

This is the bug that made every source added after AniWorld/s.to look broken:
``rebuildLanguageSelect()`` special-cased FilmPalast/MegaKino/hanime and fell
through to AniWorld's fixed language list for everything else. So a 9anime
episode was offered "German Dub", the provider map is keyed by the languages
the site really has ("English Sub"), the lookup missed, and the modal reported
"No source available" for a title whose sources were right there.

A JS contract test rather than a runtime one, in the same spirit as
tests/test_js_contracts.py: it pins the shape of the code that decides this,
so the hardcoded fallback cannot come back unnoticed.
"""

from pathlib import Path

import pytest


APP_JS = Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web" / "static" / "app.js"


@pytest.fixture(scope="module")
def app_js():
    return APP_JS.read_text(encoding="utf-8", errors="replace")


def test_unknown_sites_use_the_reported_languages(app_js):
    """The dynamic branch must exist and must be reached BEFORE ANIWORLD_LANGS."""
    dynamic = app_js.find("if (!isSto && !isAniworld) {")
    aniworld_langs = app_js.find("window.ANIWORLD_LANGS || {}")
    assert dynamic != -1, "the dynamic language branch is gone"
    assert aniworld_langs != -1
    assert dynamic < aniworld_langs, \
        "AniWorld's language set is consulted before the site's own languages"


def test_the_dynamic_branch_reads_both_sources_of_truth(app_js):
    """foundLangs (/api/episodes) with availableProviders (/api/providers) as
    the fallback -- either is an answer about THIS title."""
    start = app_js.find("if (!isSto && !isAniworld) {")
    block = app_js[start:start + 1200]
    assert "foundLangs" in block
    assert "availableProviders" in block


def test_providers_answer_triggers_a_language_rebuild(app_js):
    """The first rebuild runs before either fetch returns, so for these sites
    the dropdown starts empty -- something has to fill it afterwards."""
    start = app_js.find("async function fetchProviders(")
    assert start != -1
    block = app_js[start:start + 1400]
    assert "rebuildLanguageSelect()" in block, \
        "fetchProviders no longer rebuilds the language dropdown"


def test_movies_only_assume_german_for_the_german_only_sites(app_js):
    """filmo.to is multi-language; hardcoding "German Dub" for every movie made
    its provider lookup miss for every language the dropdown offered."""
    idx = app_js.find('availableProviders = { "German Dub": seriesData.available_providers }')
    assert idx != -1, "movie provider branch not found"
    guard = app_js[max(0, idx - 700):idx]
    assert "_germanOnlyMovieSite" in guard, \
        "the German-only assumption is applied to every movie site again"
    assert "filmpalast.to" in guard and "megakino" in guard
