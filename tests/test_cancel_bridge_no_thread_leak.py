"""Regression guard: the audio+video download's cancel bridge must not park a
thread forever.

`_ext_watcher` in models/common/common.py used to do a bare
`cancel_event.wait()`. That event belongs to a single download attempt and is
never set when the attempt succeeds, so the daemon thread stayed parked for the
lifetime of the process — one leaked thread per downloaded episode, until every
Thread.start() in the app failed with "RuntimeError: can't start new thread".
"""

import re
from pathlib import Path

COMMON = Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "models" / "common" / "common.py"


def test_cancel_bridge_waits_with_a_timeout():
    src = COMMON.read_text(encoding="utf-8")
    assert "_finished.set()" in src, "cancel bridge lost its release signal"
    # A bare wait() on the *external* cancel event can never time out.
    assert not re.search(r"\bcancel_event\.wait\(\s*\)", src), (
        "cancel_event.wait() without a timeout parks the bridge thread forever"
    )


if __name__ == "__main__":
    test_cancel_bridge_waits_with_a_timeout()
    print("ok")
