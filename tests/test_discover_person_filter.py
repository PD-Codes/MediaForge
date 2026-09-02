"""Advanced Search actor filter: the discover allow-list and the JS gating.

Two things can silently break this filter without anything failing loudly:
the allow-list dropping ``with_people`` (the search then just ignores the
actor), and the front end sending it for series, where TMDB ignores people
filters and would return an unfiltered list that *looks* filtered.
"""

import re
from pathlib import Path

from mediaforge.web.routes.search import (
    _DISCOVER_ALLOWED_PARAMS,
    _sanitise_discover_params,
)

STATIC = Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web" / "static"


def test_with_people_survives_the_allow_list():
    params, _page = _sanitise_discover_params({"with_people": "31,192"})
    assert params["with_people"] == "31,192"


def test_unknown_params_are_still_dropped():
    assert "with_borked" not in _DISCOVER_ALLOWED_PARAMS
    params, _page = _sanitise_discover_params({"with_borked": "1"})
    assert "with_borked" not in params


def test_frontend_sends_with_people_only_for_movies():
    js = (STATIC / "advanced_search.js").read_text(encoding="utf-8")
    build = js[js.index("function buildParams()"):]
    build = build[: build.index("\n  }")]
    tv_branch = build[build.index('if (S.type === "tv")'):]
    # with_people must sit in the else-branch, i.e. after the tv block opens
    # and behind an "else".
    assert "with_people" in tv_branch
    assert re.search(r"\}\s*else if \(S\.people\.length\)", tv_branch)


if __name__ == "__main__":
    test_with_people_survives_the_allow_list()
    test_unknown_params_are_still_dropped()
    test_frontend_sends_with_people_only_for_movies()
    print("ok")
