"""A captcha solve's clearance must reach every thread's session.

GLOBAL_SESSION is one session per thread and a solve runs in the download
attempt's thread, so the cf_clearance cookie and the UA it is bound to died
with that thread and every following episode was challenged again.
"""

import threading

from mediaforge import config


def _in_thread(fn):
    out = {}
    t = threading.Thread(target=lambda: out.update(result=fn()))
    t.start()
    t.join(10)
    return out.get("result")


def test_clearance_cookies_and_ua_reach_other_threads():
    before = (dict(config._CLEARANCE["cookies"]), config._CLEARANCE["ua"])
    try:
        _in_thread(lambda: config.store_clearance(
            [{"name": "cf_clearance", "value": "CLEAR", "domain": ".voe.sx"},
             {"name": "laravel_session", "value": "FLOW", "domain": "filmo.to"}],
            user_agent="Browser-UA/1.0"))

        def read():
            jar = config.GLOBAL_SESSION.cookies
            return (jar.get("cf_clearance", domain="voe.sx"),
                    jar.get("laravel_session", domain="filmo.to"),
                    config.GLOBAL_SESSION.headers.get("User-Agent"))

        assert _in_thread(read) == ("CLEAR", None, "Browser-UA/1.0")
    finally:
        with config._CLEARANCE_LOCK:
            config._CLEARANCE["cookies"] = before[0]
            config._CLEARANCE["ua"] = before[1]
            config._CLEARANCE["version"] += 1
