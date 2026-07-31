"""Comic filename parsing, against the shapes real libraries actually use.

Every case below was taken from a real 5,230-file library, not invented. That
matters: the first version of this parser looked correct against made-up names
like "Batman 001 (2011)" and fell apart on the German scans the user actually
had, which put the number after an underscore, after a dash, or in front of
everything.
"""
import pytest

from mediaforge.web.comics.identity import issue_sort_key
from mediaforge.web.comics.identity import normalize
from mediaforge.web.comics.identity import parse


@pytest.mark.parametrize("stem,series,number,title", [
    # --- the scene-standard English shapes ---
    ("Batman 001 (2011)",                    "Batman", "001", ""),
    ("Batman #1 (2011)",                     "Batman", "1",   ""),
    ("Batman v2 001 (of 12)",                "Batman", "001", ""),
    ("Batman - 001 - The Beginning",         "Batman", "001", "The Beginning"),
    ("Batman 001 (2011) (Digital) (Zone-Empire)", "Batman", "001", ""),

    # --- series, number, title (the most common German album layout) ---
    ("Asterix 001 - Asterix der Gallier",    "Asterix", "001", "Asterix der Gallier"),
    ("Bessy 001 - Das Geheimnis der sieben Feuer (1965)",
     "Bessy", "001", "Das Geheimnis der sieben Feuer"),

    # --- underscore instead of a space: 3,000 files in one library ---
    ("Bessy_326",                            "Bessy", "326", ""),

    # --- number trailing after a dash, no title at all ---
    ("Gespenster Geschichten - 0001",        "Gespenster Geschichten", "0001", ""),
    ("Gespenster Geschichten - 1655 (Tigerpress 06-2008)",
     "Gespenster Geschichten", "1655", ""),

    # --- number leading, series only in the folder ---
    ("001 - Der Kolumbusfalter und andere Abenteuer (1. Auflage)",
     "", "001", "Der Kolumbusfalter und andere Abenteuer"),

    # --- number leading AND the title carries its own dash ---
    ("261 - Jubilaeumsausgabe - Donald Duck - King of Comics",
     "", "261", "Jubilaeumsausgabe - Donald Duck - King of Comics"),

    # --- bare "series number" ---
    ("Lasso 001",                            "Lasso", "001", ""),
    ("Silberpfeil 539",                      "Silberpfeil", "539", ""),

    # --- the whole stem is the number: the series lives in the folder ---
    ("001",                                  "", "001", ""),
    ("#003",                                 "", "003", ""),

    # --- specials keep their word, decimals keep their point ---
    ("Batman Annual 01 (2012)",              "Batman", "Annual 01", ""),
    ("Spawn 1.5",                            "Spawn", "1.5", ""),

    # --- a year-like name is a name, not an issue number ---
    ("1984",                                 "1984", "", ""),
    ("einfach nur text",                     "einfach nur text", "", ""),
])
def test_parse_real_world_names(stem, series, number, title):
    got = parse(stem)
    assert got["series"] == series
    assert got["number"] == number
    assert got["title"] == title


def test_percent_encoded_umlauts_are_decoded():
    """Filenames that came through a downloader keep their %XX escapes."""
    got = parse("Buffalo Bill 379 - In der H%f6lle der Sioux")
    assert got["series"] == "Buffalo Bill"
    assert got["title"] == "In der Hölle der Sioux"


def test_volume_and_year_are_separated():
    got = parse("Saga v01 004 (2013)")
    assert (got["series"], got["number"], got["volume"], got["year"]) == ("Saga", "004", "01", 2013)


def test_issue_order_is_numeric_and_specials_sort_last():
    order = sorted(["10", "2", "1", "1.5", "0", "Annual 2", "Special"], key=issue_sort_key)
    assert order == ["0", "1", "1.5", "2", "10", "Special", "Annual 2"]


def test_zero_padding_does_not_split_a_run():
    """"001" and "1" are the same issue, so they must not sort apart."""
    assert issue_sort_key("001") == issue_sort_key("1")


def test_normalize_folds_accents_and_punctuation():
    assert normalize("Pokémon Adventures") == normalize("Pokemon  Adventures")
    assert normalize("Tom & Jerry") == normalize("Tom and Jerry")
