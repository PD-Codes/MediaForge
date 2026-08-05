"""Per-title language profiles.

The global "preferred language" setting answers the question once for an entire
library, which is the wrong granularity for the libraries this app is actually
used on: an anime collection that should be German-subbed, a series collection
that should be German-dubbed, and a handful of shows where only the original
audio is acceptable, all in the same install.

A **profile** is an ordered fallback chain (``["de", "en", "ja"]``) plus one
flag: whether the chain means "take the first one that exists" (the default) or
"take every one that exists". Titles are bound to a profile by series URL --
not by TMDB id, because a good part of the catalogue has no TMDB match and
would silently fall back to the global setting, which is exactly the surprise
this feature exists to remove.

Resolution order for a download is: rule engine action ``language_profile`` ->
explicit per-title binding -> global setting. That ordering is what makes the
rule engine useful for bulk cases ("everything from provider X uses profile Y")
without taking away per-title control.
"""

from __future__ import annotations

import json

from ..logger import get_logger

logger = get_logger(__name__)


def _loads(raw, fallback):
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else fallback
    except Exception:
        return fallback


def _row_to_profile(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "chain": _loads(row["chain"], []),
        "grab_all": bool(row["grab_all"]),
    }


def list_profiles() -> list[dict]:
    from .db import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM language_profiles ORDER BY name COLLATE NOCASE").fetchall()
        out = [_row_to_profile(r) for r in rows]
        for profile in out:
            profile["titles"] = conn.execute(
                "SELECT COUNT(*) FROM title_language_profile WHERE profile_id = ?",
                (profile["id"],)).fetchone()[0]
        return out
    except Exception:
        return []
    finally:
        conn.close()


def get_profile(profile_id: int) -> dict | None:
    from .db import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM language_profiles WHERE id = ?",
                           (profile_id,)).fetchone()
        return _row_to_profile(row) if row else None
    finally:
        conn.close()


def _clean_chain(raw) -> list[str]:
    """Normalise and de-duplicate a language chain, preserving order.

    Order is the entire meaning of the field, so ``set()`` is not an option
    here -- de-duplication has to keep the first occurrence.
    """
    if not isinstance(raw, list):
        return []
    seen, out = set(), []
    for item in raw[:12]:
        code = str(item).strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code[:16])
    return out


def save_profile(payload: dict, profile_id: int | None = None) -> tuple[int | None, str | None]:
    name = str(payload.get("name") or "").strip()
    if not name:
        return None, "name_required"
    chain = _clean_chain(payload.get("chain"))
    if not chain:
        return None, "chain_required"
    grab_all = 1 if payload.get("grab_all") else 0

    from .db import get_db
    conn = get_db()
    try:
        if profile_id:
            conn.execute(
                "UPDATE language_profiles SET name=?, chain=?, grab_all=? WHERE id=?",
                (name[:80], json.dumps(chain), grab_all, profile_id))
            conn.commit()
            return profile_id, None
        cur = conn.execute(
            "INSERT INTO language_profiles (name, chain, grab_all) VALUES (?,?,?)",
            (name[:80], json.dumps(chain), grab_all))
        conn.commit()
        return cur.lastrowid, None
    except Exception as exc:
        if "UNIQUE" in str(exc):
            return None, "duplicate_name"
        return None, str(exc)
    finally:
        conn.close()


def delete_profile(profile_id: int) -> bool:
    from .db import get_db
    conn = get_db()
    try:
        # The FK on title_language_profile declares ON DELETE CASCADE, but
        # PRAGMA foreign_keys is off on every connection in this app (see
        # db.py's _configure_connection for the reason it has to stay off),
        # so the cascade never fires and the bindings have to go explicitly.
        conn.execute("DELETE FROM title_language_profile WHERE profile_id = ?",
                     (profile_id,))
        cur = conn.execute("DELETE FROM language_profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


def bind_title(series_url: str, profile_id: int | None, title: str = "") -> bool:
    """Attach a title to a profile, or detach it when ``profile_id`` is None."""
    series_url = (series_url or "").strip()
    if not series_url:
        return False
    from .db import get_db
    conn = get_db()
    try:
        if profile_id is None:
            conn.execute("DELETE FROM title_language_profile WHERE series_url = ?",
                         (series_url,))
        else:
            conn.execute(
                "INSERT INTO title_language_profile (series_url, title, profile_id, updated_at)"
                " VALUES (?,?,?,datetime('now'))"
                " ON CONFLICT(series_url) DO UPDATE SET"
                " title=excluded.title, profile_id=excluded.profile_id,"
                " updated_at=excluded.updated_at",
                (series_url, str(title)[:300], profile_id))
        conn.commit()
        return True
    finally:
        conn.close()


def list_bindings(profile_id: int | None = None) -> list[dict]:
    from .db import get_db
    conn = get_db()
    try:
        sql = ("SELECT b.series_url, b.title, b.profile_id, b.updated_at, p.name AS profile_name "
               "FROM title_language_profile b "
               "LEFT JOIN language_profiles p ON p.id = b.profile_id")
        params = ()
        if profile_id is not None:
            sql += " WHERE b.profile_id = ?"
            params = (profile_id,)
        sql += " ORDER BY b.title COLLATE NOCASE, b.series_url"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def resolve(series_url: str, *, rule_profile_id: int | None = None,
            fallback: str = "") -> dict:
    """Decide the language chain for one download.

    Returns ``{"chain": [...], "grab_all": bool, "source": "..."}`` where
    ``source`` names which of the three levels decided -- the Dry-Run view
    shows it, and it is the first thing anyone asks when the answer surprises
    them.
    """
    profile = None
    source = "global"

    if rule_profile_id:
        profile = get_profile(rule_profile_id)
        if profile:
            source = "rule"

    if profile is None and series_url:
        from .db import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT profile_id FROM title_language_profile WHERE series_url = ?",
                (series_url,)).fetchone()
            if row:
                profile = get_profile(row["profile_id"])
                if profile:
                    source = "title"
        except Exception:
            pass
        finally:
            conn.close()

    if profile is None:
        chain = [c for c in [(fallback or "").strip().lower()] if c]
        return {"chain": chain, "grab_all": False, "source": source}

    return {"chain": profile["chain"], "grab_all": profile["grab_all"],
            "source": source, "profile": profile["name"]}
