"""Append-only audit log in its own database file.

Why a separate database
-----------------------
The audit log lives in ``audit.db``, not in ``mediaforge.db``, for three
reasons that all point the same way:

* **It must survive the main database.** Restoring a snapshot, importing a
  backup or rolling back an upgrade all replace ``mediaforge.db`` wholesale.
  An audit log inside it would be replaced too -- and the record of *who
  performed that restore* is exactly the record you least want to lose.
* **Different write pattern.** The audit log is append-only and never joined
  against anything. Keeping it out of the main file keeps its writes off the
  main database's single writer lock, which the workers already contend for.
* **Different retention.** Audit records outlive the data they describe, and
  are usually the one thing an operator wants to keep when everything else is
  disposable.

Tamper evidence
---------------
Each row stores a SHA-256 over its own content plus the previous row's hash.
That does not make the log unforgeable -- anybody with write access to the
file can rebuild the chain -- but it does make *selective* edits detectable,
which is what the log is actually asked to prove: not "nobody could have
changed this", but "nothing was quietly removed from the middle".

Writes are queued and flushed by a background thread. An audit write must
never be able to slow down or fail the request it is recording; if the queue
overflows, the drop is itself recorded rather than silently swallowed.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import queue
import sqlite3
import threading

from ..config import MEDIAFORGE_CONFIG_DIR
from ..logger import get_logger

logger = get_logger(__name__)

AUDIT_DB_PATH = MEDIAFORGE_CONFIG_DIR / "audit.db"

# Bounded on purpose. An unbounded queue turns a stuck writer into an
# out-of-memory kill of the whole app.
_MAX_QUEUE = 5000

_queue: "queue.Queue[dict]" = queue.Queue(maxsize=_MAX_QUEUE)
_writer_thread: threading.Thread | None = None
_writer_lock = threading.Lock()
_dropped = 0

# Categories are a closed set so the filter UI can be built from them and so
# that a typo does not create a category nobody ever filters on.
CATEGORIES = (
    "auth", "user", "group", "settings", "module", "backup", "queue",
    "library", "system", "api", "integration",
)

SEVERITIES = ("info", "notice", "warning", "critical")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    category    TEXT NOT NULL,
    action      TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    actor_id    INTEGER,
    actor_name  TEXT NOT NULL DEFAULT '',
    actor_type  TEXT NOT NULL DEFAULT 'user',
    target      TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    user_agent  TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '{}',
    outcome     TEXT NOT NULL DEFAULT 'success',
    prev_hash   TEXT NOT NULL DEFAULT '',
    row_hash    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts       ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_category ON audit_log (category, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor    ON audit_log (actor_id, ts DESC);
"""

# Deletion is blocked at the database level, not only in application code.
# A trigger is a weak guarantee (anybody who can drop the trigger can then
# delete) but it stops the realistic case: a future refactor, a stray helper,
# or a "let me just clean this up" query.
_GUARD = """
CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit_log
WHEN (SELECT COUNT(*) FROM audit_retention_ok) = 0
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
"""

# Retention is the one legitimate reason to delete. The pruner inserts a row
# into this table for the duration of its transaction, which is the only way
# the delete trigger above lets anything through.
_RETENTION_TABLE = """
CREATE TABLE IF NOT EXISTS audit_retention_ok (token TEXT PRIMARY KEY);
"""


def _connect() -> sqlite3.Connection:
    MEDIAFORGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUDIT_DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_audit_db() -> None:
    conn = _connect()
    try:
        conn.executescript(_RETENTION_TABLE)
        conn.executescript(_SCHEMA)
        conn.executescript(_GUARD)
        conn.commit()
    finally:
        conn.close()
    _ensure_writer()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _hash_row(prev_hash: str, payload: dict) -> str:
    material = "|".join([
        prev_hash,
        payload["ts"], payload["category"], payload["action"],
        str(payload.get("actor_id") or ""), payload.get("actor_name", ""),
        payload.get("target", ""), payload.get("outcome", ""),
        payload.get("detail", "{}"),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _ensure_writer() -> None:
    global _writer_thread
    with _writer_lock:
        if _writer_thread and _writer_thread.is_alive():
            return
        _writer_thread = threading.Thread(
            target=_writer_loop, daemon=True, name="audit-writer")
        _writer_thread.start()


def _writer_loop() -> None:
    conn = None
    while True:
        try:
            item = _queue.get()
            batch = [item]
            # Drain whatever else is waiting: a settings save writes a dozen
            # rows and there is no reason to fsync a dozen times.
            while len(batch) < 200:
                try:
                    batch.append(_queue.get_nowait())
                except queue.Empty:
                    break

            if conn is None:
                conn = _connect()

            row = conn.execute(
                "SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev = row["row_hash"] if row else ""

            for payload in batch:
                payload["prev_hash"] = prev
                prev = _hash_row(prev, payload)
                payload["row_hash"] = prev
                conn.execute(
                    "INSERT INTO audit_log (ts, category, action, severity, actor_id,"
                    " actor_name, actor_type, target, ip, user_agent, detail, outcome,"
                    " prev_hash, row_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (payload["ts"], payload["category"], payload["action"],
                     payload["severity"], payload.get("actor_id"),
                     payload.get("actor_name", ""), payload.get("actor_type", "user"),
                     payload.get("target", ""), payload.get("ip", ""),
                     payload.get("user_agent", ""), payload.get("detail", "{}"),
                     payload.get("outcome", "success"),
                     payload["prev_hash"], payload["row_hash"]),
                )
            conn.commit()
        except Exception as exc:
            logger.warning("[Audit] Writer error: %s", exc)
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None
            # Back off rather than spin: if the disk is full, retrying at full
            # speed makes the situation worse and floods the log.
            import time as _t
            _t.sleep(2)


# Keys whose values must never be written into the audit detail, even though
# the *fact* that they changed absolutely must be. Matched as substrings, so
# a new "..._api_key" setting is covered without anybody remembering to add it.
_REDACT_HINTS = ("password", "secret", "token", "api_key", "apikey", "pin",
                 "cookie", "session", "private", "credential")


def redact(data) -> dict:
    """Replace sensitive values with a marker, keeping the keys visible.

    "Someone changed the Telegram bot token at 14:02" is the useful record.
    The token itself in a log file that is deliberately never deleted is a
    second copy of a secret, in the one place nobody thinks to rotate.
    """
    if not isinstance(data, dict):
        return {"value": "<redacted>"} if data is not None else {}
    out = {}
    for key, val in data.items():
        lowered = str(key).lower()
        if any(hint in lowered for hint in _REDACT_HINTS):
            out[key] = "<redacted>" if val not in (None, "") else ""
        elif isinstance(val, dict):
            out[key] = redact(val)
        elif isinstance(val, (list, tuple)):
            out[key] = [redact(v) if isinstance(v, dict) else v for v in val][:50]
        elif isinstance(val, str) and len(val) > 500:
            out[key] = val[:500] + "…"
        else:
            out[key] = val
    return out


def _actor() -> tuple[int | None, str, str]:
    """Best-effort identity of whoever caused this. Never raises."""
    try:
        from flask import has_request_context, session
        if not has_request_context():
            return None, "system", "system"
        uid = session.get("user_id")
        # "user_name", not "username": that is the key auth.py's login() sets.
        name = session.get("user_name") or ""
        if uid is None:
            return None, name or "anonymous", "anonymous"
        return uid, name or str(uid), "user"
    except Exception:
        return None, "system", "system"


def _request_meta() -> tuple[str, str]:
    try:
        from flask import has_request_context, request
        if not has_request_context():
            return "", ""
        # request.remote_addr, not X-Forwarded-For: the header is client
        # controlled and would let anybody write an arbitrary IP into the
        # audit log. Deployments behind a proxy should use Flask's
        # ProxyFix so remote_addr is correct at the source.
        return request.remote_addr or "", (request.user_agent.string or "")[:300]
    except Exception:
        return "", ""


def audit(category: str, action: str, *, target: str = "", detail=None,
          outcome: str = "success", severity: str = "info",
          actor_id=None, actor_name=None) -> None:
    """Record one auditable event. Never raises, never blocks."""
    global _dropped
    try:
        if category not in CATEGORIES:
            category = "system"
        if severity not in SEVERITIES:
            severity = "info"

        auto_id, auto_name, actor_type = _actor()
        ip, ua = _request_meta()

        payload = {
            "ts": _dt.datetime.now().isoformat(timespec="seconds"),
            "category": category,
            "action": str(action)[:120],
            "severity": severity,
            "actor_id": auto_id if actor_id is None else actor_id,
            "actor_name": (auto_name if actor_name is None else str(actor_name))[:120],
            "actor_type": actor_type,
            "target": str(target)[:300],
            "ip": ip,
            "user_agent": ua,
            "detail": json.dumps(redact(detail), ensure_ascii=False, default=str)[:4000],
            "outcome": str(outcome)[:40],
        }

        _ensure_writer()
        try:
            _queue.put_nowait(payload)
        except queue.Full:
            _dropped += 1
            # Record the loss itself once the queue drains, so a gap in the
            # log is never invisible.
            if _dropped in (1, 10, 100, 1000):
                logger.error("[Audit] Queue full, %d event(s) dropped", _dropped)
    except Exception as exc:
        logger.debug("[Audit] Failed to record %s/%s: %s", category, action, exc)


def flush(timeout: float = 5.0) -> bool:
    """Wait for the queue to drain. Used by tests and by the export route."""
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        if _queue.empty():
            _t.sleep(0.05)  # let the in-flight batch commit
            return True
        _t.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def query(*, category: str = "", action: str = "", actor_id=None,
          search: str = "", since: str = "", until: str = "",
          severity: str = "", limit: int = 100, offset: int = 0) -> dict:
    conn = _connect()
    try:
        where, params = [], []
        if category:
            where.append("category = ?"); params.append(category)
        if severity:
            where.append("severity = ?"); params.append(severity)
        if action:
            where.append("action = ?"); params.append(action)
        if actor_id is not None:
            where.append("actor_id = ?"); params.append(actor_id)
        if since:
            where.append("ts >= ?"); params.append(since)
        if until:
            where.append("ts <= ?"); params.append(until)
        if search:
            where.append("(actor_name LIKE ? OR target LIKE ? OR action LIKE ? OR detail LIKE ?)")
            like = "%" + search + "%"
            params.extend([like, like, like, like])

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute("SELECT COUNT(*) FROM audit_log" + clause, params).fetchone()[0]

        limit = max(1, min(int(limit or 100), 500))
        offset = max(0, int(offset or 0))
        rows = conn.execute(
            "SELECT * FROM audit_log" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "entries": [_row_out(r) for r in rows],
        }
    finally:
        conn.close()


def _row_out(row) -> dict:
    out = dict(row)
    try:
        out["detail"] = json.loads(out.get("detail") or "{}")
    except Exception:
        out["detail"] = {}
    return out


def stats() -> dict:
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        by_cat = {r["category"]: r["n"] for r in conn.execute(
            "SELECT category, COUNT(*) AS n FROM audit_log GROUP BY category").fetchall()}
        oldest = conn.execute("SELECT MIN(ts) FROM audit_log").fetchone()[0]
        newest = conn.execute("SELECT MAX(ts) FROM audit_log").fetchone()[0]
        failures = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE outcome != 'success'").fetchone()[0]
        size = AUDIT_DB_PATH.stat().st_size if AUDIT_DB_PATH.exists() else 0
        return {"total": total, "by_category": by_cat, "oldest": oldest,
                "newest": newest, "failures": failures, "size": size,
                "dropped": _dropped}
    finally:
        conn.close()


def verify_chain(limit: int = 0) -> dict:
    """Recompute the hash chain and report the first row that does not match."""
    conn = _connect()
    try:
        sql = "SELECT * FROM audit_log ORDER BY id"
        rows = conn.execute(sql).fetchall() if not limit else conn.execute(
            sql + " DESC LIMIT ?", (limit,)).fetchall()[::-1]
        prev = rows[0]["prev_hash"] if rows else ""
        for row in rows:
            expected = _hash_row(prev, dict(row))
            if row["prev_hash"] != prev or row["row_hash"] != expected:
                return {"ok": False, "checked": len(rows), "broken_at": row["id"]}
            prev = row["row_hash"]
        return {"ok": True, "checked": len(rows)}
    finally:
        conn.close()


def export_csv(**filters) -> str:
    import csv
    import io
    flush(2.0)
    filters["limit"] = 500
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "ts", "category", "action", "severity", "actor",
                     "target", "ip", "outcome", "detail"])
    offset = 0
    while True:
        page = query(**{**filters, "offset": offset})
        for entry in page["entries"]:
            writer.writerow([
                entry["id"], entry["ts"], entry["category"], entry["action"],
                entry["severity"], entry["actor_name"], entry["target"],
                entry["ip"], entry["outcome"],
                json.dumps(entry["detail"], ensure_ascii=False),
            ])
        offset += page["limit"]
        if offset >= page["total"]:
            break
    return buf.getvalue()


def prune(keep_days: int) -> int:
    """Delete entries older than ``keep_days``. The only sanctioned delete.

    Opens the retention gate the delete trigger checks, prunes, and closes it
    again in the same transaction so the gate is never left open.
    """
    if keep_days <= 0:
        return 0
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=keep_days)).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT OR REPLACE INTO audit_retention_ok (token) VALUES ('open')")
        cur = conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM audit_retention_ok")
        conn.commit()
        removed = cur.rowcount or 0
        if removed:
            logger.info("[Audit] Pruned %d entries older than %d days", removed, keep_days)
        return removed
    except Exception as exc:
        conn.rollback()
        logger.warning("[Audit] Prune failed: %s", exc)
        return 0
    finally:
        conn.close()
