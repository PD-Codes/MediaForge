"""The per-process anchor connection and the lock-free idle claims (P23-01, P23-09c).

Run in a subprocess with its own config directory: the session's app runs
background workers that call the claim functions and get_db() all the time, so
patching DB_PATH in-process would race with them.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

SCRIPT = r"""
import json, os, sqlite3, time
from mediaforge.web import db
from mediaforge.web.db import _core

out = {}
db.init_app_settings_db(); db.init_queue_db()
db.init_encoding_queue_db(); db.init_upscale_queue_db()
wal = str(_core.DB_PATH) + "-wal"

# Not armed yet (no run_pending() in this process): old behaviour, the last
# close removes the WAL.
db.get_setting("x")
out["anchor_before_arm"] = _core._anchor is not None
out["wal_before_arm"] = os.path.exists(wal)

db.arm_db_anchor()
db.get_setting("x")
out["anchor_open"] = _core._anchor is not None
out["anchor_in_txn"] = _core._anchor.in_transaction
for i in range(20):                       # worker-style short connections
    db.update_queue_progress(1, i, "")
out["wal_kept"] = os.path.exists(wal)

probe = _core._configure_connection(sqlite3.connect(str(_core.DB_PATH)))
out["journal_size_limit"] = probe.execute("PRAGMA journal_size_limit").fetchone()[0]
out["synchronous"] = probe.execute("PRAGMA synchronous").fetchone()[0]
# The idle anchor must not pin a WAL snapshot: a full checkpoint completes.
out["checkpoint"] = list(probe.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
probe.close()

# Another connection holds the write lock; claims with nothing queued must
# answer at once instead of waiting for it.
holder = sqlite3.connect(str(_core.DB_PATH), timeout=30)
holder.execute("BEGIN IMMEDIATE")
t0 = time.monotonic()
out["idle_claims"] = [db.claim_next_queued(), db.claim_next_encoding_queued(),
                      db.claim_next_upscale_queued()]
out["idle_claim_seconds"] = time.monotonic() - t0
holder.rollback(); holder.close()

qid = db.add_to_queue("T", "https://example.org/x", ["e1"], "German Dub", "VOE")
eid = db.add_to_encoding_queue("E", "/tmp/e.mkv")
uid = db.add_to_upscale_queue("U", "/tmp/u.mkv")
out["claimed"] = [(db.claim_next_queued() or {}).get("id") == qid,
                  (db.claim_next_encoding_queued() or {}).get("id") == eid,
                  (db.claim_next_upscale_queued() or {}).get("id") == uid]
out["claimed_twice"] = [db.claim_next_queued(), db.claim_next_encoding_queued(),
                        db.claim_next_upscale_queued()]
db.optimize_db()
check = sqlite3.connect(str(_core.DB_PATH))
out["stats"] = bool(check.execute(
    "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone())
out["upscale_status_index"] = bool(check.execute(
    "SELECT 1 FROM sqlite_master WHERE name = 'idx_upscale_queue_status'").fetchone())
check.close()
print(json.dumps(out))
"""


def test_anchor_and_idle_claims(tmp_path):
    env = dict(os.environ, MEDIAFORGE_CONFIG_DIR=str(tmp_path), PYTHONPATH=str(SRC))
    proc = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["anchor_before_arm"] is False
    assert out["wal_before_arm"] is False
    assert out["anchor_open"] is True
    assert out["anchor_in_txn"] is False
    assert out["wal_kept"] is True
    assert out["journal_size_limit"] == 67108864
    assert out["synchronous"] == 1                    # NORMAL
    assert out["checkpoint"][0] == 0                  # not blocked by the anchor
    assert out["idle_claims"] == [None, None, None]
    assert out["idle_claim_seconds"] < 2.0            # no wait for the write lock
    assert out["claimed"] == [True, True, True]
    assert out["claimed_twice"] == [None, None, None]
    assert "stats" in out                             # optimize_db() ran (no forced ANALYZE)
    assert out["upscale_status_index"] is True
