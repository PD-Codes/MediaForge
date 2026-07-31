"""The comic scanner's grouping rules, on a real-world folder layout."""
import zipfile

import pytest

from mediaforge.web.comics.scanner import scan_comics


def _cbz(path, pages=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(pages):
            zf.writestr(f"page{i + 1}.jpg", b"\xff\xd8\xff\xe0not-really-a-jpeg")
    return path


def test_folder_is_the_series_even_when_filenames_disagree(tmp_path):
    """One folder per run is a statement; 500 filenames are 500 chances to drift.

    These four spellings all occur in one real library. Grouping on the
    filename produced four shelves; the folder is the answer the user gave.
    """
    folder = tmp_path / "Die tollsten Geschichten von Donald Duck"
    _cbz(folder / "Die tollsten Geschichten von Donald Duck 388.cbz")
    _cbz(folder / "Die tollsten Geschichten mit Donald Duck 457.cbz")
    _cbz(folder / "Die tollsten Geschichten von Donald Duck - Sonderheft 001 (Ehapa 1965).cbz")
    _cbz(folder / "Die tollsten Geschichten von Donald Duck 389.cbz")

    result = scan_comics(tmp_path)
    assert len(result) == 1
    series = result[0]
    assert series["series"] == "Die tollsten Geschichten von Donald Duck"
    assert series["issue_count"] == 4
    assert [i["number"] for i in series["issues"]] == ["001", "388", "389", "457"]


def test_separate_folders_stay_separate_series(tmp_path):
    _cbz(tmp_path / "Asterix" / "Asterix 001 - Asterix der Gallier.cbz")
    _cbz(tmp_path / "Bessy" / "Bessy_326.cbz")
    result = scan_comics(tmp_path)
    assert sorted(s["series"] for s in result) == ["Asterix", "Bessy"]
    assert [i["number"] for s in result if s["series"] == "Bessy" for i in s["issues"]] == ["326"]


def test_loose_files_in_the_root_group_by_filename(tmp_path):
    """With no folder to go on, the filename is all there is."""
    _cbz(tmp_path / "Lasso 001.cbz")
    _cbz(tmp_path / "Lasso 002.cbz")
    _cbz(tmp_path / "Silberpfeil 001.cbz")
    result = scan_comics(tmp_path)
    by_name = {s["series"]: s for s in result}
    assert sorted(by_name) == ["Lasso", "Silberpfeil"]
    assert by_name["Lasso"]["issue_count"] == 2


def test_a_title_that_only_repeats_the_series_is_dropped(tmp_path):
    _cbz(tmp_path / "Bessy" / "Bessy 001 - Bessy.cbz")
    issue = scan_comics(tmp_path)[0]["issues"][0]
    assert not issue.get("title")


def test_issue_keys_are_stable_and_unique(tmp_path):
    """Reading progress hangs off these, so two files must never share one."""
    _cbz(tmp_path / "Bessy" / "Bessy 001 - A.cbz")
    _cbz(tmp_path / "Bessy" / "Bessy 001 - B.cbz")
    issues = scan_comics(tmp_path)[0]["issues"]
    assert len({i["key"] for i in issues}) == 2


def test_unreadable_containers_are_listed_not_hidden(tmp_path):
    """A CBR that needs an unpacker still belongs on the shelf, flagged."""
    folder = tmp_path / "Bessy"
    folder.mkdir(parents=True)
    (folder / "Bessy 001.cbr").write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 128)
    series = scan_comics(tmp_path)[0]
    assert series["issue_count"] == 1
    assert series["needs_conversion_count"] == 1
    assert series["issues"][0]["readable"] is False


def test_a_cbr_that_is_really_a_zip_is_read_without_any_rar_tool(tmp_path):
    """The single most common real-world mislabelling."""
    _cbz(tmp_path / "Asterix" / "Asterix 001.cbr")
    issue = scan_comics(tmp_path)[0]["issues"][0]
    assert issue["readable"] is True
    assert not issue.get("needs_conversion")
    assert issue["format_label"] == "CBZ"


def test_empty_fields_are_omitted_from_issue_rows(tmp_path):
    """Issue rows carry only what they actually say.

    This whole dict is cached as JSON and shipped on every shelf load. On a
    5,230-issue library the omitted keys were 1.1 MB of "characters": [] and
    "needs_conversion": false. Consumers read these with `issue.get(...)`, so
    absent and empty mean the same thing to them -- but a future field that
    quietly defaults to something other than falsy would break that, which is
    what this test is here to catch.
    """
    _cbz(tmp_path / "Asterix" / "Asterix 001.cbz")
    issue = scan_comics(tmp_path)[0]["issues"][0]

    for gone in ("series", "summary", "publisher", "writers", "characters",
                 "language", "volume", "year", "title", "rtl", "page_count",
                 "direct", "needs_conversion"):
        assert gone not in issue, f"{gone} should have been dropped"

    # ... while the fields something depends on stay, falsy or not.
    for kept in ("path", "file", "key", "number", "size", "readable"):
        assert kept in issue, f"{kept} must not be dropped"
