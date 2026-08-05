# Provider contract fixtures

Recorded HTML pages from the supported streaming sites, used by
`tests/test_provider_contracts.py` to detect a layout change **before** users
report one.

## Why this exists

A provider breaks in a very specific way: the site quietly changes its markup,
the parser stops finding episodes, and every Auto-Sync job starts reporting
"could not read the series page". Nothing in the codebase changed, so nothing
in CI noticed. The first signal is an issue from somebody whose downloads
stopped three days ago.

Two tests close that gap, and they are deliberately different:

* **Offline (`pytest`, runs everywhere).** Parses the fixtures in this folder
  and asserts the parser still extracts what it used to. This catches *our*
  regressions — a refactor that breaks episode extraction fails the PR that
  introduced it. It never touches the network, so it is fast and works in CI
  and offline.
* **Live (`MEDIAFORGE_CONTRACT_LIVE=1`, scheduled).** Fetches the real page and
  checks the *shape* of what comes back. This catches *their* changes. It is
  not part of the normal suite: a site being down would otherwise fail an
  unrelated pull request, which is exactly how a check like this gets disabled.

## Recording a fixture

```bash
python tests/contracts/record.py aniworld https://aniworld.to/anime/stream/<slug>
```

The recorder strips cookies and scripts, and writes `<provider>.html` plus a
`<provider>.json` describing what the parser found at recording time. The JSON
is the contract — the HTML is only the input that produced it.

## What not to record

* Nothing from an adult provider. The fixtures are committed to a public
  repository.
* Nothing personalised. The recorder makes an anonymous request and refuses to
  save a page that contains a `logout` link, which is the cheapest reliable
  sign that a session leaked into it.

## When a fixture goes stale

That is the point. If the live check fails and the offline one passes, the
site changed: fix the parser, re-record the fixture, and the offline test now
guards the new shape.
