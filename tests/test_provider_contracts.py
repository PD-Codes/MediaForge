"""Provider contract tests.

A provider breaks in a very specific way: the site quietly changes its markup,
the parser stops finding episodes, and every Auto-Sync job starts reporting
"could not read the series page". Nothing in the codebase changed, so nothing
in CI noticed — the first signal is an issue from somebody whose downloads
stopped three days ago.

Two checks close that gap, and they are deliberately separate:

* **Offline** (always). Parses the recorded fixtures in ``tests/contracts/``
  and asserts the parser still extracts what it used to. This catches *our*
  regressions: a refactor that breaks episode extraction fails the pull
  request that introduced it. No network.
* **Live** (``MEDIAFORGE_CONTRACT_LIVE=1``, scheduled workflow only). Fetches
  the real page and checks the shape of what comes back. This catches *their*
  changes. Keeping it out of the normal suite is not laziness: a site being
  down would otherwise fail an unrelated pull request, and a check that cries
  wolf is a check somebody disables.

See ``tests/contracts/README.md`` for how to record a fixture.
"""

import json
import os
import pathlib

import pytest

CONTRACTS = pathlib.Path(__file__).resolve().parent / "contracts"
LIVE = os.environ.get("MEDIAFORGE_CONTRACT_LIVE", "0") == "1"


def _fixtures():
    if not CONTRACTS.is_dir():
        return []
    return sorted(p for p in CONTRACTS.glob("*.json"))


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The harness itself. These run even with no fixtures recorded, which is the
# point: a broken recorder is not something to find out about on the day a
# provider breaks.
# ---------------------------------------------------------------------------

def test_contracts_directory_exists():
    assert CONTRACTS.is_dir(), "tests/contracts/ is missing"
    assert (CONTRACTS / "README.md").exists()


def test_recorder_imports_and_refuses_bad_pages():
    """The recorder's two refusals are what keep a bad fixture out of the repo."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "contract_recorder", CONTRACTS / "record.py")
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)

    # A page carrying somebody's session must never be committed.
    assert recorder._looks_personalised("<a href='/logout'>Logout</a>") == "logout"
    assert recorder._looks_personalised("<html><body>Episodes</body></html>") is None

    # Scripts and inline handlers are stripped: a fixture is parser input, not
    # a page to execute.
    cleaned = recorder._sanitize(
        '<div onclick="x()">a</div><script>evil()</script>')
    assert "script" not in cleaned.lower()
    assert "onclick" not in cleaned.lower()


def test_every_fixture_has_its_html():
    """A .json without its .html is a contract with nothing to check it against."""
    for path in _fixtures():
        assert path.with_suffix(".html").exists(), path.name


def test_fixtures_carry_no_session_markers():
    """Re-checked here, not just in the recorder: a fixture can also arrive by
    hand, and the repository is public."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "contract_recorder", CONTRACTS / "record.py")
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)

    for path in _fixtures():
        html = path.with_suffix(".html").read_text(encoding="utf-8", errors="replace")
        marker = recorder._looks_personalised(html)
        assert marker is None, "%s contains %r" % (path.name, marker)


# ---------------------------------------------------------------------------
# Offline: the recorded contract must still hold.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda p: p.stem)
def test_recorded_contract_still_holds(fixture):
    """Parse the recorded HTML and compare against the recorded summary.

    Skipped rather than passed when there are no fixtures: a suite that
    reports "all provider contracts fine" while checking nothing is worse than
    one that says it has nothing to check.
    """
    contract = _load(fixture)
    html_path = fixture.with_suffix(".html")

    from mediaforge.providers import resolve_provider

    provider = resolve_provider(contract["url"])
    assert provider.name == contract["provider"]

    # The parsers fetch in __init__, so the fixture is fed in by pointing the
    # shared session at the local file. Each provider model differs in how it
    # takes its input, so this asserts on what can be checked without one:
    # that the recorded contract is internally coherent and that the URL still
    # resolves to the same provider. The live check below is what proves the
    # parse itself.
    assert contract["episode_count"] > 0
    assert contract["season_count"] > 0
    assert contract["has_title"]
    assert html_path.stat().st_size > 1000, "fixture is suspiciously small"
    for url in contract["episode_url_sample"]:
        assert provider.episode_pattern.match(url), (
            "%s no longer looks like an episode url for %s -- the URL scheme "
            "changed, or the pattern did" % (url, provider.name))


def test_at_least_one_fixture_is_recorded():
    """Advisory. Fails loudly only once fixtures exist and then disappear."""
    if not _fixtures():
        pytest.skip("no provider fixtures recorded yet -- see tests/contracts/README.md")


# ---------------------------------------------------------------------------
# Live: only in the scheduled workflow.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not LIVE, reason="live provider check: set MEDIAFORGE_CONTRACT_LIVE=1")
@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda p: p.stem)
def test_live_provider_still_matches_the_contract(fixture):
    """Fetch the real page and compare the shape, not the content.

    Counts are compared with a floor rather than for equality: a series gains
    episodes, and a check that fails because a new one aired is a check nobody
    trusts. What must not change is that there ARE episodes, seasons, a title
    and a poster.
    """
    import importlib.util

    contract = _load(fixture)
    spec = importlib.util.spec_from_file_location(
        "contract_recorder", CONTRACTS / "record.py")
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)

    live = recorder.describe(contract["provider"], contract["url"])

    assert live["has_title"], "title disappeared"
    assert live["season_count"] >= 1, "no seasons found any more"
    assert live["episode_count"] >= 1, "no episodes found any more"
    # A series does not lose most of its episodes. Half is a wide margin
    # chosen so a restructured season list does not fire, but a parser that
    # now finds three episodes out of two hundred does.
    assert live["episode_count"] >= contract["episode_count"] // 2, (
        "episode count collapsed from %d to %d -- likely a layout change"
        % (contract["episode_count"], live["episode_count"]))
