"""Rule engine: conditional overrides for how a download is handled.

Today every decision about a download -- target quality, whether to encode it
afterwards, which folder it lands in -- is a single global switch. That works
until the first exception, and there is always a first exception: this one
anime should stay 1080p, that documentary collection belongs in a different
folder, downloads from one provider should never be upscaled because the
source is already clean.

A rule is ``conditions -> actions``. Rules are evaluated in priority order
against a *context* dict describing the pending download; matching rules merge
their actions, later ones overriding earlier ones, unless a rule sets ``stop``
in which case evaluation ends there.

Two deliberate limits keep this from growing into a scripting language nobody
can debug:

* Conditions are a flat AND-list. No nesting, no OR. "Two different cases"
  is two rules, which is also how a person describes it.
* Actions are a fixed, validated set. An action key this build does not know
  is dropped on save rather than stored, for the same reason unknown
  permissions are dropped in :mod:`mediaforge.web.groups`: a setting that
  displays as configured and does nothing is worse than one that was refused.

The engine is pure -- :func:`evaluate` touches no database and has no side
effects -- so it is cheap to call on the download path and trivial to test.
"""

from __future__ import annotations

import json
import re

from ..logger import get_logger

logger = get_logger(__name__)

# Context fields a condition may test, with the type the comparison uses.
# Everything is compared as a lower-cased string except the numeric ones.
FIELDS: dict[str, str] = {
    "title":       "text",
    "provider":    "text",
    "language":    "text",
    "media_type":  "text",   # series | movie | anime | comic | book
    "genre":       "text",
    "season":      "number",
    "episode":     "number",
    "year":        "number",
    "url":         "text",
    "requested_by": "text",
}

OPERATORS: dict[str, str] = {
    "is":          "op_is",
    "is_not":      "op_is_not",
    "contains":    "op_contains",
    "not_contains": "op_not_contains",
    "starts_with": "op_starts_with",
    "matches":     "op_matches",       # regular expression
    "gt":          "op_gt",
    "lt":          "op_lt",
}

# Actions and their validators. The value returned by the validator is what
# gets stored, so this doubles as normalisation.
def _v_bool(val):
    return bool(val) if isinstance(val, bool) else str(val).lower() in ("1", "true", "yes", "on")


def _v_str(val):
    return str(val)[:400]


def _v_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _v_quality(val):
    allowed = {"best", "2160p", "1080p", "720p", "480p", "worst"}
    val = str(val).lower()
    return val if val in allowed else None


ACTIONS: dict[str, tuple[str, callable]] = {
    "quality":          ("action_quality", _v_quality),
    "language":         ("action_language", _v_str),
    "target_folder":    ("action_target_folder", _v_str),
    "language_profile": ("action_language_profile", _v_int),
    "priority":         ("action_priority", _v_int),
    "encode_after":     ("action_encode_after", _v_bool),
    "upscale_after":    ("action_upscale_after", _v_bool),
    "download_subtitles": ("action_subtitles", _v_bool),
    "skip":             ("action_skip", _v_bool),
    "notify":           ("action_notify", _v_bool),
}

# A regular expression from a rule runs against a title, on the download path.
# Catastrophic backtracking there would hang the worker, so patterns are length
# capped and compiled once at evaluation with a plain re -- no lookbehind-heavy
# constructs are blocked outright, but the cap keeps the damage bounded.
_MAX_PATTERN = 200


def _as_text(value) -> str:
    return "" if value is None else str(value).strip().lower()


def _as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _test(condition: dict, context: dict) -> bool:
    field = condition.get("field")
    operator = condition.get("op")
    expected = condition.get("value")
    if field not in FIELDS or operator not in OPERATORS:
        # Unknown condition fails closed: a rule nobody can evaluate must not
        # silently become "always true" and start applying its actions.
        return False

    actual = context.get(field)

    if FIELDS[field] == "number" or operator in ("gt", "lt"):
        left, right = _as_number(actual), _as_number(expected)
        if left is None or right is None:
            return False
        if operator == "gt":
            return left > right
        if operator == "lt":
            return left < right
        if operator == "is":
            return left == right
        if operator == "is_not":
            return left != right
        return False

    left, right = _as_text(actual), _as_text(expected)
    if operator == "is":
        return left == right
    if operator == "is_not":
        return left != right
    if operator == "contains":
        return right in left
    if operator == "not_contains":
        return right not in left
    if operator == "starts_with":
        return left.startswith(right)
    if operator == "matches":
        if len(right) > _MAX_PATTERN:
            return False
        try:
            return re.search(right, left, re.IGNORECASE) is not None
        except re.error:
            # An invalid pattern is a configuration mistake, not a match.
            return False
    return False


def evaluate(context: dict, rules: list[dict] | None = None) -> dict:
    """Return the merged actions for ``context``.

    The result also carries ``_matched`` (rule names, in order) so the dry-run
    view can explain *why* a download would be handled a certain way -- an
    engine that only reports its verdict is one nobody trusts.
    """
    rules = list_rules() if rules is None else rules
    merged: dict = {}
    matched: list[str] = []

    for rule in rules:
        if not rule.get("enabled", True):
            continue
        conditions = rule.get("conditions") or []
        # A rule with no conditions matches everything. That is useful as a
        # default-setter at low priority, and it is what the UI shows it as.
        if conditions and not all(_test(c, context) for c in conditions):
            continue
        matched.append(rule.get("name", "?"))
        merged.update(rule.get("actions") or {})
        if rule.get("stop"):
            break

    merged["_matched"] = matched
    return merged


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _clean_conditions(raw) -> list[dict]:
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        field, operator = item.get("field"), item.get("op")
        if field not in FIELDS or operator not in OPERATORS:
            continue
        out.append({"field": field, "op": operator,
                    "value": str(item.get("value", ""))[:400]})
    return out


def _clean_actions(raw) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        spec = ACTIONS.get(key)
        if not spec:
            continue
        cleaned = spec[1](value)
        if cleaned is not None:
            out[key] = cleaned
    return out


def _row_to_rule(row) -> dict:
    def _loads(raw, fallback):
        try:
            return json.loads(raw)
        except Exception:
            return fallback
    return {
        "id": row["id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "priority": row["priority"],
        "stop": bool(row["stop"]),
        "conditions": _loads(row["conditions"], []),
        "actions": _loads(row["actions"], {}),
    }


def list_rules() -> list[dict]:
    from .db import get_db
    conn = get_db()
    try:
        return [_row_to_rule(r) for r in conn.execute(
            "SELECT * FROM download_rules ORDER BY priority, id").fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def get_rule(rule_id: int) -> dict | None:
    from .db import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM download_rules WHERE id = ?",
                           (rule_id,)).fetchone()
        return _row_to_rule(row) if row else None
    finally:
        conn.close()


def save_rule(payload: dict, rule_id: int | None = None) -> tuple[int | None, str | None]:
    name = str(payload.get("name") or "").strip()
    if not name:
        return None, "name_required"
    conditions = json.dumps(_clean_conditions(payload.get("conditions")))
    actions = json.dumps(_clean_actions(payload.get("actions")))
    enabled = 1 if payload.get("enabled", True) else 0
    stop = 1 if payload.get("stop") else 0
    try:
        priority = max(0, min(int(payload.get("priority", 100)), 9999))
    except (TypeError, ValueError):
        priority = 100

    from .db import get_db
    conn = get_db()
    try:
        if rule_id:
            conn.execute(
                "UPDATE download_rules SET name=?, enabled=?, priority=?, stop=?,"
                " conditions=?, actions=?, updated_at=datetime('now') WHERE id=?",
                (name[:120], enabled, priority, stop, conditions, actions, rule_id))
            conn.commit()
            return rule_id, None
        cur = conn.execute(
            "INSERT INTO download_rules (name, enabled, priority, stop, conditions, actions)"
            " VALUES (?,?,?,?,?,?)",
            (name[:120], enabled, priority, stop, conditions, actions))
        conn.commit()
        return cur.lastrowid, None
    finally:
        conn.close()


def delete_rule(rule_id: int) -> bool:
    from .db import get_db
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM download_rules WHERE id = ?", (rule_id,))
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()
