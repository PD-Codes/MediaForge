"""User groups: permissions plus library scoping.

Model
-----
The app shipped with three fixed roles -- ``admin``, ``user``, ``kids`` --
checked by name in decorators (:func:`mediaforge.web.auth.admin_required` and
friends). That works, but it has no middle: to let somebody approve downloads
you had to make them an admin, which also handed them the settings, the module
store and every other account.

A **group** bundles two things that always travelled together anyway:

* a set of **permissions** (``library.read``, ``queue.write``, ...), and
* a **library scope**: which library locations its members may see at all.

Keeping them in one object is deliberate. The alternative -- permissions on a
group, scope assigned separately per user -- means every "why can this person
not see that folder?" question has two places to look, and they drift.

Compatibility
-------------
``users.role`` is untouched and remains the source of truth for the three
built-in groups, which are materialised in this table with ``builtin = 1`` and
cannot be deleted or renamed. A user's effective permission set is the union of

* the built-in group matching their ``role``, and
* every custom group they have been added to.

That makes this purely additive for existing installs: nobody loses access on
upgrade, and ``admin_required`` keeps working unchanged because ``admin`` still
holds the wildcard permission.

Library scope works slightly differently, and the difference matters. The
built-in groups all carry the wildcard scope, because that is what every
account had before this existed. If the wildcard simply won, scoping would be
permanently dead: every user is in a built-in group, so every user would keep
seeing everything no matter which scoped group you added them to.

So an *explicit* scope beats the wildcard: as soon as any of a user's groups
names concrete locations, the user is restricted to the union of those names.
Only a user whose groups all say "everything" gets everything. Admins are
never scoped -- an admin who cannot see a library cannot fix it either.
"""

from __future__ import annotations

import json

from ..logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Permission catalogue
# ---------------------------------------------------------------------------
# Every permission the app knows, with the i18n key used to label it in the UI.
# Adding one here is all that is needed for it to appear in the group editor --
# do not hardcode a second list in a template.
#
# The wildcard "*" is not listed: it is not something an admin picks from a
# menu, it is what the built-in admin group has.

PERMISSIONS: dict[str, str] = {
    "library.read":      "perm_library_read",
    "library.write":     "perm_library_write",
    "library.delete":    "perm_library_delete",
    "queue.read":        "perm_queue_read",
    "queue.write":       "perm_queue_write",
    "queue.cancel":      "perm_queue_cancel",
    "search.use":        "perm_search_use",
    "download.request":  "perm_download_request",
    "download.approve":  "perm_download_approve",
    "autosync.read":     "perm_autosync_read",
    "autosync.write":    "perm_autosync_write",
    "encoding.use":      "perm_encoding_use",
    "upscale.use":       "perm_upscale_use",
    "player.stream":     "perm_player_stream",
    "reading.use":       "perm_reading_use",
    "favourites.use":    "perm_favourites_use",
    "stats.read":        "perm_stats_read",
    "modules.read":      "perm_modules_read",
    "modules.install":   "perm_modules_install",
    "settings.read":     "perm_settings_read",
    "settings.write":    "perm_settings_write",
    "users.manage":      "perm_users_manage",
    "audit.read":        "perm_audit_read",
    "ops.read":          "perm_ops_read",
    "ops.manage":        "perm_ops_manage",
    "adult.view":        "perm_adult_view",
}

WILDCARD = "*"

# The three built-in groups. Keys match `users.role` values exactly -- that
# mapping is what keeps the old decorators working.
BUILTIN_GROUPS: dict[str, dict] = {
    "admin": {
        "name": "Administrator",
        "permissions": [WILDCARD],
        "scope": [WILDCARD],
    },
    "user": {
        "name": "User",
        "permissions": [
            "library.read", "queue.read", "queue.write", "queue.cancel",
            "search.use", "download.request", "autosync.read",
            "player.stream", "reading.use", "favourites.use", "stats.read",
            "modules.read", "adult.view",
        ],
        "scope": [WILDCARD],
    },
    # 'kids' is a RESTRICTION, not a rank. Note the absence of adult.view --
    # the age gate is expressed here as a permission rather than as another
    # role-name comparison scattered through the routes.
    "kids": {
        "name": "Kids",
        "permissions": [
            "library.read", "search.use", "player.stream", "reading.use",
            "favourites.use",
        ],
        "scope": [WILDCARD],
    },
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def create_schema(conn) -> None:
    """Create the group tables and seed the built-ins. Called by migration 2."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT NOT NULL UNIQUE,
            name        TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            builtin     INTEGER NOT NULL DEFAULT 0,
            permissions TEXT NOT NULL DEFAULT '[]',
            -- JSON array of library location ids, or ["*"] for everything.
            scope       TEXT NOT NULL DEFAULT '["*"]',
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_group_members (
            user_id  INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, group_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_members_user "
        "ON user_group_members (user_id)")

    for key, spec in BUILTIN_GROUPS.items():
        conn.execute(
            "INSERT OR IGNORE INTO user_groups "
            "(key, name, builtin, permissions, scope) VALUES (?,?,1,?,?)",
            (key, spec["name"], json.dumps(spec["permissions"]),
             json.dumps(spec["scope"])),
        )


def init_groups_db() -> None:
    """Idempotent bootstrap for the no-migration path (fresh installs).

    The migration engine covers upgrades; this covers a brand-new database,
    where ``init_*_db()`` is what builds the world. Both are safe to run.
    """
    from .db import get_db
    conn = get_db()
    try:
        create_schema(conn)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _row_to_group(row) -> dict:
    return {
        "id": row["id"],
        "key": row["key"],
        "name": row["name"],
        "description": row["description"],
        "builtin": bool(row["builtin"]),
        "permissions": _loads(row["permissions"], []),
        "scope": _loads(row["scope"], [WILDCARD]),
    }


def _loads(raw, fallback):
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else fallback
    except Exception:
        return fallback


def list_groups() -> list[dict]:
    from .db import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM user_groups ORDER BY builtin DESC, name COLLATE NOCASE"
        ).fetchall()
        out = []
        for row in rows:
            group = _row_to_group(row)
            group["members"] = conn.execute(
                "SELECT COUNT(*) FROM user_group_members WHERE group_id = ?",
                (group["id"],)).fetchone()[0]
            out.append(group)
        return out
    finally:
        conn.close()


def get_group(group_id: int) -> dict | None:
    from .db import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM user_groups WHERE id = ?",
                           (group_id,)).fetchone()
        return _row_to_group(row) if row else None
    finally:
        conn.close()


def user_group_ids(user_id: int) -> list[int]:
    from .db import get_db
    conn = get_db()
    try:
        return [r[0] for r in conn.execute(
            "SELECT group_id FROM user_group_members WHERE user_id = ?",
            (user_id,)).fetchall()]
    finally:
        conn.close()


def _groups_for_user(conn, user_id: int, role: str) -> list[dict]:
    """Built-in group for the role, plus every custom group joined."""
    rows = conn.execute(
        "SELECT * FROM user_groups WHERE key = ? "
        "UNION "
        "SELECT g.* FROM user_groups g "
        "JOIN user_group_members m ON m.group_id = g.id WHERE m.user_id = ?",
        (role or "user", user_id),
    ).fetchall()
    return [_row_to_group(r) for r in rows]


def effective_permissions(user_id: int, role: str) -> set[str]:
    """Union of every permission the user's groups grant.

    Returns ``{"*"}`` for anybody in a wildcard group -- callers should use
    :func:`has_permission` rather than testing membership themselves, so the
    wildcard is handled in exactly one place.
    """
    from .db import get_db
    conn = get_db()
    try:
        perms: set[str] = set()
        for group in _groups_for_user(conn, user_id, role):
            perms.update(group["permissions"])
        return perms
    except Exception as exc:
        # A database that has not been migrated yet must not lock everyone
        # out. Fall back to the built-in definition for the role.
        logger.debug("[Groups] Falling back to builtin permissions: %s", exc)
        return set(BUILTIN_GROUPS.get(role or "user", BUILTIN_GROUPS["user"])["permissions"])
    finally:
        try:
            conn.close()
        except Exception:
            pass


def effective_scope(user_id: int, role: str) -> list[str]:
    """Library locations the user may see, or ``["*"]`` for all of them.

    An explicit scope beats the wildcard -- see the module docstring for why
    the obvious "wildcard wins" rule would make scoping a no-op for everyone.
    """
    if role == "admin":
        return [WILDCARD]
    from .db import get_db
    conn = get_db()
    try:
        explicit: set[str] = set()
        for group in _groups_for_user(conn, user_id, role):
            explicit.update(s for s in group["scope"] if s != WILDCARD)
        return sorted(explicit) if explicit else [WILDCARD]
    except Exception:
        return [WILDCARD]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def scope_allows(scope, location_id: str) -> bool:
    """True when a library location is inside the given effective scope."""
    if not scope or WILDCARD in scope:
        return True
    return str(location_id) in {str(s) for s in scope}


def has_permission(user_id: int, role: str, permission: str) -> bool:
    if role == "admin":
        # Short-circuit: admins are admins even if the group tables are
        # missing, mid-migration, or somebody edited the admin group badly.
        return True
    perms = effective_permissions(user_id, role)
    return WILDCARD in perms or permission in perms


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _clean_permissions(values) -> list[str]:
    """Keep only permissions this build knows about.

    Unknown strings are dropped rather than stored: a permission that no code
    checks is a permission that looks granted in the UI and does nothing, and
    that is how people end up believing an account is restricted when it is
    not. The wildcard is refused outright -- it belongs to the built-in admin
    group and must not be assignable from a form.
    """
    if not isinstance(values, list):
        return []
    return sorted({v for v in values if isinstance(v, str) and v in PERMISSIONS})


def _clean_scope(values) -> list[str]:
    if not isinstance(values, list) or not values:
        return [WILDCARD]
    if WILDCARD in values:
        return [WILDCARD]
    return sorted({str(v) for v in values if str(v).strip()}) or [WILDCARD]


def create_group(key: str, name: str, permissions=None, scope=None,
                 description: str = "") -> tuple[int | None, str | None]:
    from .db import get_db
    key = (key or "").strip().lower().replace(" ", "_")
    name = (name or "").strip()
    if not key or not name:
        return None, "invalid_name"
    if key in BUILTIN_GROUPS:
        return None, "reserved_key"

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO user_groups (key, name, description, builtin, permissions, scope) "
            "VALUES (?,?,?,0,?,?)",
            (key, name, description,
             json.dumps(_clean_permissions(permissions)),
             json.dumps(_clean_scope(scope))),
        )
        conn.commit()
        return cur.lastrowid, None
    except Exception as exc:
        if "UNIQUE" in str(exc):
            return None, "duplicate_key"
        return None, str(exc)
    finally:
        conn.close()


def update_group(group_id: int, *, name=None, description=None,
                 permissions=None, scope=None) -> tuple[bool, str | None]:
    from .db import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM user_groups WHERE id = ?",
                           (group_id,)).fetchone()
        if not row:
            return False, "not_found"
        if row["builtin"] and (permissions is not None or scope is not None):
            # Built-in groups define what the three roles mean app-wide and
            # are relied on by the decorators. Editing their permissions from
            # the UI would let an admin lock themselves out of the settings
            # page that fixes it.
            return False, "builtin_readonly"

        fields, params = [], []
        if name is not None and not row["builtin"]:
            fields.append("name = ?"); params.append(str(name).strip())
        if description is not None:
            fields.append("description = ?"); params.append(str(description))
        if permissions is not None:
            fields.append("permissions = ?")
            params.append(json.dumps(_clean_permissions(permissions)))
        if scope is not None:
            fields.append("scope = ?")
            params.append(json.dumps(_clean_scope(scope)))
        if not fields:
            return True, None

        params.append(group_id)
        conn.execute("UPDATE user_groups SET %s WHERE id = ?" % ", ".join(fields), params)
        conn.commit()
        return True, None
    finally:
        conn.close()


def delete_group(group_id: int) -> tuple[bool, str | None]:
    from .db import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT builtin FROM user_groups WHERE id = ?",
                           (group_id,)).fetchone()
        if not row:
            return False, "not_found"
        if row["builtin"]:
            return False, "builtin_readonly"
        conn.execute("DELETE FROM user_group_members WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM user_groups WHERE id = ?", (group_id,))
        conn.commit()
        return True, None
    finally:
        conn.close()


def set_user_groups(user_id: int, group_ids) -> tuple[bool, str | None]:
    from .db import get_db
    conn = get_db()
    try:
        valid = {r[0] for r in conn.execute(
            "SELECT id FROM user_groups WHERE builtin = 0").fetchall()}
        wanted = {int(g) for g in (group_ids or []) if str(g).isdigit()} & valid
        conn.execute("DELETE FROM user_group_members WHERE user_id = ?", (user_id,))
        for gid in wanted:
            conn.execute(
                "INSERT OR IGNORE INTO user_group_members (user_id, group_id) VALUES (?,?)",
                (user_id, gid))
        conn.commit()
        return True, None
    finally:
        conn.close()


def purge_user(user_id: int) -> None:
    """Drop a deleted user's memberships.

    Called from ``delete_user()``. There is no foreign key here for the same
    reason ``user_ui_prefs`` has none (the no-auth pseudo-user id 0), so the
    cleanup has to be explicit.
    """
    from .db import get_db
    conn = get_db()
    try:
        conn.execute("DELETE FROM user_group_members WHERE user_id = ?", (user_id,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
