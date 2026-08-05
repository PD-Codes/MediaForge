#!/usr/bin/env python3
"""Record a provider contract fixture.

    python tests/contracts/record.py aniworld https://aniworld.to/anime/stream/<slug>

Writes two files next to this script:

* ``<provider>.html`` -- the page, with scripts and inline event handlers
  stripped. It is only the *input*; nothing asserts anything about it.
* ``<provider>.json`` -- what the parser extracted from it. This is the
  contract, and it is what ``tests/test_provider_contracts.py`` compares
  against on every run.

Deliberately a standalone script rather than a pytest fixture: recording hits
the real site and is a thing a maintainer does on purpose, occasionally. Tests
that can rewrite their own expectations do not fail.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

# Markers that mean the page was fetched with somebody's session attached.
# A fixture is committed to a public repository, so this check is not optional.
_PERSONAL_MARKERS = ("logout", "abmelden", "mein konto", "my account",
                     "profil bearbeiten", "csrf")

_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.IGNORECASE | re.DOTALL)
_ON_ATTR_RE = re.compile(r'\son[a-z]+\s*=\s*("[^"]*"|\'[^\']*\')', re.IGNORECASE)


def _sanitize(html: str) -> str:
    html = _SCRIPT_RE.sub("", html)
    return _ON_ATTR_RE.sub("", html)


def _looks_personalised(html: str) -> str | None:
    lowered = html.lower()
    for marker in _PERSONAL_MARKERS:
        if marker in lowered:
            return marker
    return None


def describe(provider_name: str, url: str) -> dict:
    """Run the real parser and summarise what it found.

    A summary, not a dump: the contract is "this page still yields a title, a
    poster, N seasons and M episodes whose urls look like episode urls". Pinning
    the exact episode titles would make the fixture fail every time the site
    fixed a typo, and a check that cries wolf gets deleted.
    """
    from mediaforge.providers import resolve_provider

    provider = resolve_provider(url)
    series = provider.series_cls(url)

    seasons = list(getattr(series, "seasons", []) or [])
    episodes = []
    for season in seasons:
        episodes.extend(list(getattr(season, "episodes", []) or []))

    return {
        "provider": provider.name,
        "url": url,
        "has_title": bool(getattr(series, "title", "")),
        "has_poster": bool(getattr(series, "poster_url", "")),
        "has_description": bool(getattr(series, "description", "")),
        "season_count": len(seasons),
        "episode_count": len(episodes),
        "episode_url_sample": [str(getattr(e, "url", e)) for e in episodes[:3]],
        "has_movies": bool(getattr(series, "has_movies", False)),
    }


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print(__doc__)
        return 2
    name, url = argv[0].strip().lower(), argv[1].strip()

    from mediaforge.config import GLOBAL_SESSION

    print("Fetching %s …" % url)
    resp = GLOBAL_SESSION.get(url, timeout=30)
    resp.raise_for_status()
    html = _sanitize(resp.text)

    marker = _looks_personalised(html)
    if marker:
        print("REFUSED: the page contains %r, which usually means a session "
              "leaked into it. Fetch it logged out." % marker)
        return 1

    contract = describe(name, url)
    if not contract["episode_count"]:
        print("REFUSED: the parser found no episodes. Recording that would "
              "pin the broken state as the expected one.")
        return 1

    (HERE / ("%s.html" % name)).write_text(html, encoding="utf-8")
    (HERE / ("%s.json" % name)).write_text(
        json.dumps(contract, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("Recorded %s.html + %s.json" % (name, name))
    print(json.dumps(contract, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
