"""The repository checks CI runs, available locally through pytest as well.

.github/scripts/check_repo.py is the single implementation; this only wires it
into the test suite so `pytest` catches the same things before a push does.
The checks are pure file inspection -- no network, no app, milliseconds each.
"""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "check_repo.py"


@pytest.fixture(scope="module")
def check_repo():
    if not _SCRIPT.is_file():
        pytest.skip("check_repo.py is missing (source checkout without .github/)")
    spec = importlib.util.spec_from_file_location("check_repo", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_templates_only_reference_existing_static_files(check_repo):
    problems = check_repo.check_assets()
    assert not problems, "\n  " + "\n  ".join(problems)


def test_translation_catalogs_are_compiled(check_repo):
    problems = check_repo.check_catalog()
    if problems and "babel is not installed" in problems[0]:
        pytest.skip(problems[0])
    assert not problems, "\n  " + "\n  ".join(problems)


def test_no_file_is_stored_with_crlf(check_repo):
    problems = check_repo.check_eol()
    assert not problems, "\n  " + "\n  ".join(problems)
