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


def test_prefetch_pool_is_shared_and_bounded():
    """The autosync provider_data pool must be one long-lived pool, not a new one
    per sync run: a pool only spawns a worker when it has fewer than max_workers,
    so a shared pool creates at most five threads for the whole process lifetime
    and every later submit just queues behind a free worker.
    """
    import threading

    from mediaforge.web import autosync_worker as aw

    pool = aw._get_pd_pool()
    assert aw._get_pd_pool() is pool, "pool must be a singleton, not per call"
    assert pool._max_workers == 5

    done = threading.Event()
    futures = [pool.submit(lambda: done.wait(0.05)) for _ in range(50)]
    for f in futures:
        f.result(timeout=30)
    # 50 tasks, never more than 5 worker threads -- they queued instead. Count
    # the pool's own threads, not threading.active_count(): background threads
    # left by other tests come and go during the run and made this flaky.
    assert len(pool._threads) <= 5


if __name__ == "__main__":
    test_cancel_bridge_waits_with_a_timeout()
    test_prefetch_pool_is_shared_and_bounded()
    print("ok")
