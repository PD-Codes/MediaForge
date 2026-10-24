"""Request-body limits (web/app.py: _BodyLimitedRequest).

Bodies used to be unlimited at the Flask level (and up to 1 GiB at the
waitress level), so any JSON endpoint could be made to read hundreds of MB into
memory. Ordinary endpoints now stop at 16 MB; only the backup upload may send
more.
"""

import json

from mediaforge.web import app as app_module


def _big_json(size):
    # A valid JSON document of roughly ``size`` bytes.
    return json.dumps({"file": "x" * size})


def test_ordinary_endpoint_rejects_oversized_body(as_user):
    client = as_user("admin")
    resp = client.put(
        "/api/settings",
        data=_big_json(app_module._DEFAULT_BODY_LIMIT + 1024),
        content_type="application/json",
    )
    assert resp.status_code == 413
    assert resp.is_json
    assert resp.get_json()["error"]


def test_backup_upload_may_exceed_the_default_limit(as_user):
    client = as_user("admin")
    resp = client.post(
        "/api/backup/preview",
        data=_big_json(app_module._DEFAULT_BODY_LIMIT + 1024),
        content_type="application/json",
    )
    # The body is read and parsed (and then rejected as not being a backup),
    # instead of being cut off by the request limit.
    assert resp.status_code != 413


def test_limit_applies_before_csrf_reads_the_body(app, as_user):
    # CSRFProtect looks for a form token in a before_request hook, which
    # creates the input stream -- and the stream applies the limit right then.
    # The endpoint-specific limit therefore has to be in place before any hook.
    client = as_user("admin")
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        big = _big_json(app_module._DEFAULT_BODY_LIMIT + 1024)
        backup = client.post("/api/backup/preview", data=big,
                             content_type="application/json")
        other = client.post("/api/backup/export", data=big,
                            content_type="application/json")
    finally:
        app.config["WTF_CSRF_ENABLED"] = False
    assert backup.status_code != 413
    assert other.status_code == 413


def test_per_request_override_still_works(app):
    with app.test_request_context("/api/backup/preview", method="POST"):
        from flask import request
        request.max_content_length = 10
        assert request.max_content_length == 10
