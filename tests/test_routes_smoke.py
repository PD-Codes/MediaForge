"""Smoke test: every GET route answers without blowing up.

Cheap but effective on a codebase with 270+ routes and no tests: it catches
template errors, missing imports inside view functions, typos in url_for() and
anything else that only shows up when the route is actually called.

Nothing here asserts on content -- only that the server does not answer 500.
"""

import pytest


def _plain_get_rules(app):
    """GET rules without URL parameters, minus the ones with side effects."""
    skip_endpoints = {
        "static",
        "service_worker",
        # Talks to the outside world or starts long-running work.
        "api_update_check",
        "api_store_catalog",
        "api_store_restart",
        "api_extensions_rescan",
        "api_mediascan_refresh",
        "api_library_refresh",
        "api_uptime_check_now",
        "api_image_proxy",
        "api_crunchyroll_availability",
        "api_fernsehserien_availability",
    }
    for rule in app.url_map.iter_rules():
        if rule.arguments or "GET" not in rule.methods:
            continue
        if rule.endpoint in skip_endpoints or rule.endpoint.startswith("auth."):
            continue
        yield rule


def test_there_are_routes(app):
    """Guards the test itself: an empty rule list would make everything pass."""
    assert len(list(_plain_get_rules(app))) > 50


# Statuses that mean "a third-party site let us down", not "this route is
# broken". The browse endpoints reach out to aniworld/megakino/filmpalast, so
# without them the suite would fail whenever a source site is unreachable --
# which, on CI, it regularly is. A crash still surfaces: an unhandled
# exception answers 500, and that is not in this set.
_UPSTREAM_STATUSES = {502, 503, 504}


def test_every_get_route_answers(app, as_user):
    """No route raises. 200/3xx/4xx are all fine -- 5xx is not."""
    client = as_user("admin")
    broken = []
    for rule in _plain_get_rules(app):
        try:
            resp = client.get(str(rule))
            if resp.status_code >= 500 and resp.status_code not in _UPSTREAM_STATUSES:
                broken.append(f"{rule.endpoint} ({rule}) -> {resp.status_code}")
        except Exception as exc:  # a raised exception is the same defect
            broken.append(f"{rule.endpoint} ({rule}) -> raised {type(exc).__name__}: {exc}")
    assert not broken, "routes failing with a server error:\n  " + "\n  ".join(broken)


def test_anonymous_requests_are_redirected_or_rejected(app, client):
    """Nothing but the documented exempt set answers without a session."""
    exempt_prefixes = ("/login", "/setup", "/auth/", "/static/", "/health", "/sw.js")
    leaked = []
    for rule in _plain_get_rules(app):
        path = str(rule)
        if path.startswith(exempt_prefixes):
            continue
        resp = client.get(path)
        if resp.status_code == 200:
            leaked.append(f"{rule.endpoint} ({path})")
    # /api/syncplay/rooms and the calendar feed are login-exempt by design;
    # they are listed in web/app.py's _exempt set with a reason.
    # Login-exempt by design; each is listed in web/app.py's _exempt set with a
    # reason (guests reach the SyncPlay lobby before they have an account).
    # /readyz joins api_health for the same reason: a container orchestrator,
    # a Docker HEALTHCHECK or an external uptime monitor has no session and
    # never will. It answers a single status string -- no version, no worker
    # names, no error text -- so an unauthenticated caller learns only whether
    # the process is up. (/healthz is already covered by the "/health" prefix
    # in exempt_prefixes above.)
    # api_v1_openapi is the machine-readable description of the external API.
    # A client cannot know which scopes to ask for until it can read the spec,
    # so requiring a key to fetch it is a chicken-and-egg problem. It describes
    # shapes -- endpoint names, parameter types, required scopes -- not data.
    # offline_page is precached by the service worker at install time, when
    # the fetch carries no session worth relying on. A login redirect cached
    # under that URL would then be shown as "you are offline" forever. The
    # page contains no data at all -- that is the whole point of it.
    known_exempt = {"api_syncplay_rooms", "api_syncplay_config", "api_health",
                    "api_calendar_ics", "syncplay_page", "readyz",
                    "api_v1_openapi", "offline_page"}
    unexpected = [x for x in leaked if x.split(" ")[0] not in known_exempt]
    assert not unexpected, "reachable without a login:\n  " + "\n  ".join(unexpected)
