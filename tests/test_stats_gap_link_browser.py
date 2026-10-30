"""Opt-in browser tests for the dashboard's complete gap list."""

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from jinja2 import Environment

WEB = Path(__file__).resolve().parents[1] / "src/mediaforge/web"
pytestmark = pytest.mark.skipif(
    os.environ.get("MEDIAFORGE_BROWSER_TESTS") != "1", reason="opt-in browser tests",
)


@pytest.fixture()
def stats_page():
    from patchright.sync_api import sync_playwright

    body = Environment().from_string(
        (WEB / "templates/_stats_body.html").read_text(encoding="utf-8") +
        (WEB / "templates/_gap_fill_modal.html").read_text(encoding="utf-8"),
    ).render(_=lambda s: s)
    css = "\n".join((WEB / "static" / name).read_text(encoding="utf-8") for name in (
        "variables.css", "base.css", "forms.css", "buttons.css", "cards.css",
        "modals.css", "stats.css", "shared_modals.css", "home_gaps.css",
    ))
    # Omit media statistics deliberately: the direct link must not require them.
    payload = {"general": {}, "queue": {}, "sync": {}}
    items = [{"folder": f"series-{i}", "title": f"Series {i:02}", "location": "Test",
              "missing": [f"S1E{n}" for n in range(1, 28)]} for i in range(25)]
    requests = []
    state = {"downloads": [], "ignored": [], "failures": set()}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, executable_path=os.environ.get("MEDIAFORGE_TEST_BROWSER"),
            timeout=15000,
        )
        page = browser.new_page()

        def respond(route):
            url = urlparse(route.request.url)
            if url.path == "/stats":
                route.fulfill(content_type="text/html", body=
                              '<meta charset="utf-8"><style>' + css + '</style>' + body +
                              '<script>window.t=(de,en)=>de;</script>' +
                              '<script src="/static/mf-charts.js"></script>' +
                              '<script src="/static/gap_downloads.js"></script>' +
                              '<script src="/static/home_gaps.js"></script>' +
                              '<script src="/static/gap_batch.js"></script>' +
                              '<script src="/static/stats.js"></script>')
            elif url.path.startswith("/static/"):
                route.fulfill(content_type="application/javascript", body=
                              (WEB / "static" / url.path.rsplit("/", 1)[-1]).read_text(encoding="utf-8"))
            elif url.path == "/api/stats":
                route.fulfill(content_type="application/json", body=json.dumps(payload))
            elif url.path == "/api/media/incomplete":
                query = parse_qs(url.query)
                requests.append(query)
                number = int(query.get("page", ["1"])[0])
                size = int(query.get("per_page", ["20"])[0])
                route.fulfill(content_type="application/json", body=json.dumps({
                    "items": items[(number - 1) * size:number * size],
                    "page": number, "per_page": size, "total": len(items), "total_pages": 2,
                    "summary": {"incomplete": 25, "ignored": 0, "complete": 0, "missing_slots": 675},
                }))
            elif url.path == "/api/search/sources":
                route.fulfill(json={"sources": [{"id": "test", "label": "Test", "enabled": True}]})
            elif url.path == "/api/settings/client":
                route.fulfill(json={})
            elif url.path == "/api/search":
                title = route.request.post_data_json["keyword"]
                route.fulfill(json={"results": [{"title": title, "url": "https://source.test/" + title}]})
            elif url.path == "/api/seasons":
                route.fulfill(json={"seasons": [{"season_number": 1, "url": "https://source.test/s1"}]})
            elif url.path == "/api/episodes":
                route.fulfill(json={"episodes": [{
                    "episode_number": n, "url": f"https://source.test/e{n}",
                    "languages": ["German Dub", "English Sub"],
                } for n in range(1, 28)]})
            elif url.path == "/api/providers":
                route.fulfill(json={"providers": {"German Dub": ["VOE"], "English Sub": ["Filemoon"]}})
            elif url.path == "/api/download":
                data = route.request.post_data_json
                state["downloads"].append(data)
                route.fulfill(status=403 if data["title"] in state["failures"] else 200,
                              json={"error": "not permitted"} if data["title"] in state["failures"] else {"queue_id": 42})
            elif url.path == "/api/media/ignore":
                state["ignored"].append(route.request.post_data_json)
                route.fulfill(json={"ok": True})
            else:
                route.fulfill(status=404, body="")

        page.route("**/*", respond)
        yield page, requests, state
        browser.close()


def test_direct_link_opens_all_gap_pages_and_all_episode_slots(stats_page):
    page, requests, _ = stats_page
    page.goto("http://mediaforge.test/stats#incomplete")
    page.locator("#incompleteBody tr").first.wait_for()
    assert page.locator("#incompleteModal").is_visible()
    assert page.locator("#incompleteBody tr").count() == 20
    assert page.locator("#incompleteBody tr").first.locator(".ignore-slot-chip").count() == 27
    page.locator('#incompleteTableHost .mf-pagination-page[data-page="2"]').click()
    page.wait_for_function("document.querySelectorAll('#incompleteBody tr').length === 5")
    assert "Series 24" in page.locator("#incompleteBody").inner_text()
    assert [r["page"] for r in requests] == [["1"], ["2"]]


def test_plain_statistics_page_keeps_gap_modal_closed(stats_page):
    page, requests, _ = stats_page
    page.goto("http://mediaforge.test/stats")
    page.locator("#statsContent .stats-hint").first.wait_for()
    assert page.locator("#incompleteModal").is_hidden()
    assert not requests


def test_hash_link_resets_previous_filters_and_ignored_tab(stats_page):
    page, requests, _ = stats_page
    page.goto("http://mediaforge.test/stats")
    page.locator("#statsContent .stats-hint").first.wait_for()
    page.evaluate("""() => {
        window._incompleteView = 'ignored';
        _modalView.incomplete.q = 'old search';
        _modalView.incomplete.page = 2;
        location.hash = 'incomplete';
    }""", isolated_context=False)
    page.locator("#incompleteBody tr").first.wait_for()
    assert page.locator("#incompleteModal").is_visible()
    assert requests[0]["page"] == ["1"]
    assert requests[0]["view"] == ["incomplete"]
    assert "q" not in requests[0]


def select_gaps_on_two_pages(page):
    page.goto("http://mediaforge.test/stats#incomplete")
    page.locator('#incompleteBody tr[data-folder="series-0"] .ign-slot[data-slot="S1E2"]').check()
    page.locator('#incompleteTableHost .mf-pagination-page[data-page="2"]').click()
    page.locator('#incompleteBody tr[data-folder="series-20"] .ign-slot[data-slot="S1E4"]').check()
    page.locator("#gapDownloadSelectedBtn").click()
    page.locator("#gapBatchSubmit:enabled").wait_for()


def test_gap_selection_across_pages_queues_exact_selected_slots(stats_page):
    page, _, state = stats_page
    select_gaps_on_two_pages(page)
    assert page.locator("#gapBatchEntries li").count() == 2
    page.locator("#gapBatchSubmit").click()
    page.wait_for_function("document.querySelector('#gapBatchSubmit').textContent === 'Abgeschlossen'")
    assert [(d["title"], d["episodes"]) for d in state["downloads"]] == [
        ("Series 00", ["https://source.test/e2"]), ("Series 20", ["https://source.test/e4"]),
    ]
    assert not state["ignored"]
    assert page.evaluate("_ignSelectionCount()", isolated_context=False) == 0


def test_failed_series_remains_selected_for_retry(stats_page):
    page, _, state = stats_page
    state["failures"].add("Series 20")
    select_gaps_on_two_pages(page)
    page.locator("#gapBatchSubmit").click()
    page.wait_for_function("document.querySelector('#gapBatchSubmit').textContent === 'Abgeschlossen'")
    assert page.evaluate("_ignSelectionCount()", isolated_context=False) == 1
    assert page.evaluate("_ignSel.slots['series-20']", isolated_context=False) == ["S1E4"]
    assert "not permitted" in page.locator("#gapBatchEntries").inner_text()


def test_gap_list_opens_individual_series_with_episode_checkboxes(stats_page):
    page, _, state = stats_page
    page.goto("http://mediaforge.test/stats#incomplete")
    page.locator('#incompleteBody tr[data-folder="series-0"] button').click()
    page.locator("#gapFillSubmit:enabled").wait_for()
    page.locator("#gapFillSelectAll").uncheck()
    page.locator("#gapFillEpisodes input").nth(1).check()
    page.locator("#gapFillEpisodes input").nth(3).check()
    page.locator("#gapFillSubmit").click()
    page.wait_for_function("document.querySelector('#gapFillStatus').textContent.includes('hinzugefügt')")
    assert state["downloads"][0]["episodes"] == ["https://source.test/e2", "https://source.test/e4"]


def test_ignore_action_still_works_independently_of_downloads(stats_page):
    page, _, state = stats_page
    page.goto("http://mediaforge.test/stats#incomplete")
    page.locator('#incompleteBody tr[data-folder="series-0"] .ign-slot[data-slot="S1E2"]').check()
    page.locator("#ignApplyBtn").click()
    page.wait_for_function("document.querySelector('#incompleteModal').style.display !== 'none'")
    assert state["ignored"] == [{"items": [{"folder": "series-0", "title": "Series 00", "slots": ["S1E2"]}]}]
    assert not state["downloads"]


def test_whole_series_can_be_reduced_to_individual_missing_episodes(stats_page):
    page, _, state = stats_page
    page.goto("http://mediaforge.test/stats#incomplete")
    row = page.locator('#incompleteBody tr[data-folder="series-0"]')
    row.locator(".ign-series").check()
    assert row.locator(".ign-slot:checked").count() == 27
    row.locator('.ign-slot[data-slot="S1E3"]').uncheck()
    assert row.locator(".ign-series").evaluate("el => el.indeterminate")
    page.locator("#gapDownloadSelectedBtn").click()
    page.locator("#gapBatchSubmit:enabled").wait_for()
    page.locator("#gapBatchSubmit").click()
    page.wait_for_function("document.querySelector('#gapBatchSubmit').textContent === 'Abgeschlossen'")
    assert state["downloads"][0]["episodes"] == [
        f"https://source.test/e{n}" for n in range(1, 28) if n != 3
    ]


def test_closing_series_window_keeps_parent_gap_list_open(stats_page):
    page, _, _ = stats_page
    page.goto("http://mediaforge.test/stats#incomplete")
    page.locator('#incompleteBody tr[data-folder="series-0"] button').click()
    page.locator("#gapFillSubmit:enabled").wait_for()
    page.keyboard.press("Escape")
    assert page.locator("#gapFillOverlay").is_hidden()
    assert page.locator("#incompleteModal").is_visible()
