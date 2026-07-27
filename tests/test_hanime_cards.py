"""Card building and listing fill-up for the hanime source.

The catalogue backend changed: one plain GET returns the entire catalogue, so
filtering, sorting and paging all happen locally now (see the long note in
models/hanime_tv/scraper.py). These tests pin the two behaviours that broke
when it did -- which artwork ends up on a card, and how many cards a filtered
listing produces.

No network: the module-level catalogue cache is filled directly, which is the
same thing a successful fetch would have done.
"""

import time

import pytest


@pytest.fixture()
def scraper():
    from mediaforge.models.hanime_tv import scraper as module

    return module


def _entry(index, censored):
    """One catalogue entry, shaped like the live backend's."""
    return {
        "id": index,
        "name": f"Title {index} 1",           # per-episode name, "1" suffix
        "slug": f"title-{index}",
        "created_at_unix": index,
        "views": index,
        "cover_url": f"https://hanime-cdn.com/covers/{index}.jpg",
        "poster_url": f"https://hanime-cdn.com/posters/{index}.jpg",
        "tags": [{"text": "censored" if censored else "uncensored"}],
    }


@pytest.fixture()
def catalogue(scraper):
    """Fill the catalogue cache and restore it afterwards."""
    previous = dict(scraper._catalog_cache)

    def _fill(entries):
        scraper._catalog_cache["entries"] = entries
        scraper._catalog_cache["ts"] = time.monotonic()

    yield _fill
    scraper._catalog_cache.update(previous)


def test_cards_use_the_portrait_cover(scraper):
    """cover_url is the portrait artwork, poster_url a 16:9 scene still.

    The fields swapped meaning on the new backend, which put a cropped scene
    on every card while the detail modal showed the right image.
    """
    card = scraper._hit_to_card(_entry(1, censored=False))
    assert card["poster_url"] == "https://hanime-cdn.com/covers/1.jpg"


def test_cards_fall_back_to_poster_url(scraper):
    """Entries without a cover still need artwork."""
    hit = _entry(1, censored=False)
    del hit["cover_url"]
    assert scraper._hit_to_card(hit)["poster_url"].endswith("/posters/1.jpg")


def test_listing_fills_up_when_a_filter_removes_entries(scraper, catalogue):
    """A filtered listing must still fill the row, not leave a half-empty grid.

    Half the catalogue is censored here, so a single fixed-size page would
    have yielded half a row -- and scroll arrows with nothing to scroll.
    """
    catalogue([_entry(i, censored=bool(i % 2)) for i in range(200)])

    unfiltered = scraper.fetch_new()
    censored_off = scraper.fetch_new(show_censored=False)
    uncensored_off = scraper.fetch_new(show_uncensored=False)

    assert len(unfiltered) == scraper._LISTING_TARGET_COUNT
    assert len(censored_off) == scraper._LISTING_TARGET_COUNT
    assert len(uncensored_off) == scraper._LISTING_TARGET_COUNT
    assert {card["censored"] for card in censored_off} == {"Uncensored"}
    assert {card["censored"] for card in uncensored_off} == {"Censored"}


def test_listing_returns_what_exists_when_the_catalogue_is_short(scraper, catalogue):
    """Fewer matches than the target is not an error."""
    catalogue([_entry(i, censored=bool(i % 2)) for i in range(20)])

    cards = scraper.fetch_new(show_censored=False)

    assert 0 < len(cards) < scraper._LISTING_TARGET_COUNT


def test_listing_is_sorted_and_deduplicated_by_franchise(scraper, catalogue):
    """Newest first, and one card per franchise across the whole listing."""
    entries = [_entry(i, censored=False) for i in range(50)]
    # Two more episodes of the very first franchise, further down the sort.
    entries.append({**_entry(0, censored=False), "name": "Title 0 2", "slug": "title-0-2"})
    entries.append({**_entry(0, censored=False), "name": "Title 0 3", "slug": "title-0-3"})
    catalogue(entries)

    cards = scraper.fetch_new()

    assert cards[0]["title"] == "Title 49 1"        # highest created_at_unix
    franchises = [card["franchise"] for card in cards]
    assert len(franchises) == len(set(franchises))


def test_trending_and_new_order_differently(scraper, catalogue):
    """The two listings must not silently become the same list."""
    entries = []
    for i in range(30):
        hit = _entry(i, censored=False)
        hit["views"] = 30 - i          # reverse of created_at_unix
        entries.append(hit)
    catalogue(entries)

    assert scraper.fetch_new()[0]["title"] != scraper.fetch_trending()[0]["title"]
