"""Scoped API keys for the external REST API.

Before this there was exactly one key, stored in ``app_settings`` as
``external_api_key``, and it was all-or-nothing: whatever could read
``/api/v1/status`` could also read the whole library, the download history and
every Auto-Sync job. A Home Assistant dashboard that wants a queue count and a
script that manages downloads got identical credentials, and revoking one
meant revoking both.

This module adds keys that are:

* **Scoped.** A key carries a set of scopes; each endpoint declares the one it
  needs. A dashboard key gets ``status:read`` and nothing else.
* **Hashed.** Only a SHA-256 of the key is stored. The plaintext is shown once,
  at creation, and is unrecoverable afterwards -- a stolen database must not
  hand the attacker working credentials. This is the reason the old key could
  not simply grow a scopes column: it is stored in clear text by design,
  because the settings page displays it.
* **Individually revocable and expirable.**

The legacy key keeps working, with every scope, and is not deprecated here.
Breaking every existing Home Assistant integration to introduce scopes would
be a poor trade; it is offered as an upgrade, not imposed as one.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import secrets

from ..logger import get_logger

logger = get_logger(__name__)

# Scopes, with the i18n key used to label them. Coarse on purpose -- one scope
# per resource, read and write split only where writing is genuinely a
# different decision. A scope catalogue nobody can hold in their head gets
# used as "tick everything".
SCOPES: dict[str, str] = {
    "status:read":   "scope_status_read",
    "queue:read":    "scope_queue_read",
    "queue:write":   "scope_queue_write",
    "library:read":  "scope_library_read",
    "history:read":  "scope_history_read",
    "stats:read":    "scope_stats_read",
    "autosync:read": "scope_autosync_read",
    "uptime:read":   "scope_uptime_read",
    "update:read":   "scope_update_read",
}

WILDCARD = "*"

# Keys are shown with this prefix so they are recognisable in a log or a
# config file, and so a leaked one can be searched for.
KEY_PREFIX = "mf_"
_KEY_BYTES = 32


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _clean_scopes(values) -> list[str]:
    """Drop scopes this build does not know.

    Same rule as permissions and rule actions elsewhere: a scope nothing
    checks looks granted and does nothing, which is how somebody ends up
    believing a key is restricted when it is not. The wildcard is refused --
    it belongs to the legacy key, not to something created from a form.
    """
    if not isinstance(values, list):
        return []
    return sorted({v for v in values if isinstance(v, str) and v in SCOPES})


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_key(name: str, scopes=None, *, expires_at: str | None = None,
               user_id: int | None = None) -> tuple[str | None, str | None]:
    """Create a key and return ``(plaintext, None)`` or ``(None, error)``.

    The plaintext is returned exactly once, here. Nothing stores it.
    """
    name = (name or "").strip()
    if not name:
        return None, "name_required"
    cleaned = _clean_scopes(scopes)
    if not cleaned:
        return None, "scopes_required"

    plaintext = KEY_PREFIX + secrets.token_urlsafe(_KEY_BYTES)

    from .db import get_db
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix, scopes, user_id,"
            " enabled, expires_at) VALUES (?,?,?,?,?,1,?)",
            (name[:80], _hash(plaintext), plaintext[:len(KEY_PREFIX) + 6],
             json.dumps(cleaned), user_id, expires_at),
        )
        conn.commit()
        return plaintext, None
    except Exception as exc:
        logger.warning("[API keys] Could not create %r: %s", name, exc)
        return None, str(exc)
    finally:
        conn.close()


def list_keys() -> list[dict]:
    """Every key, without anything that could reconstruct one."""
    from .db import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, name, key_prefix, scopes, user_id, enabled, created_at,"
            " expires_at, last_used FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            try:
                entry["scopes"] = json.loads(entry["scopes"])
            except Exception:
                entry["scopes"] = []
            entry["enabled"] = bool(entry["enabled"])
            entry["expired"] = _is_expired(entry.get("expires_at"))
            out.append(entry)
        return out
    except Exception:
        return []
    finally:
        conn.close()


def set_enabled(key_id: int, enabled: bool) -> bool:
    from .db import get_db
    conn = get_db()
    try:
        cur = conn.execute("UPDATE api_keys SET enabled = ? WHERE id = ?",
                           (1 if enabled else 0, key_id))
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


def delete_key(key_id: int) -> bool:
    from .db import get_db
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _is_expired(expires_at) -> bool:
    if not expires_at:
        return False
    try:
        return _dt.datetime.fromisoformat(str(expires_at)) < _dt.datetime.now()
    except Exception:
        # An unparseable expiry is treated as expired. The alternative --
        # ignoring it -- means a malformed date silently makes a key eternal.
        return True


def verify(presented: str) -> dict | None:
    """Resolve a presented key to ``{"id", "name", "scopes"}`` or None.

    Looks the key up by hash, so the comparison is an indexed equality on a
    digest rather than a scan with ``compare_digest`` per row. That is not a
    timing shortcut: the value being compared is already a hash of the secret,
    and an attacker who can distinguish digests still cannot invert one.
    """
    if not presented:
        return None

    from .db import get_db
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name, scopes, enabled, expires_at FROM api_keys WHERE key_hash = ?",
            (_hash(presented),)).fetchone()
        if not row or not row["enabled"] or _is_expired(row["expires_at"]):
            return None
        try:
            scopes = json.loads(row["scopes"])
        except Exception:
            scopes = []
        # Best effort, and deliberately not fatal: a read-only replica or a
        # locked database must not turn a valid key into an invalid one.
        try:
            conn.execute("UPDATE api_keys SET last_used = ? WHERE id = ?",
                         (_dt.datetime.now().isoformat(timespec="seconds"), row["id"]))
            conn.commit()
        except Exception:
            pass
        return {"id": row["id"], "name": row["name"], "scopes": scopes}
    except Exception as exc:
        logger.debug("[API keys] Verification failed: %s", exc)
        return None
    finally:
        conn.close()


def has_scope(granted, needed: str) -> bool:
    if not needed:
        return True
    return WILDCARD in (granted or []) or needed in (granted or [])
