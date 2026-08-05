"""User accounts, roles and SSO identities.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

import os
import sqlite3
from werkzeug.security import check_password_hash
from werkzeug.security import generate_password_hash
from ...logger import get_logger

from ._core import USER_ROLES, _CREATE_SSO_INDEX, _CREATE_TABLE, _migrate_db, acquire_instance_lock, get_db
from .settings import get_setting
from .ui_prefs import clear_user_ui_prefs

logger = get_logger(__name__)


def init_db():
    """Create the users table (and migrate it) and auto-create an admin
    account from MEDIAFORGE_WEB_ADMIN_USER/PASS env vars if none exists yet.

    Used by: mediaforge/web/app.py (create_app, only when auth is enabled).
    """
    acquire_instance_lock()
    conn = get_db()
    try:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_SSO_INDEX)
        conn.commit()
        _migrate_db(conn)
    finally:
        conn.close()

    if not has_any_admin():
        env_user = os.environ.get("MEDIAFORGE_WEB_ADMIN_USER", "").strip()
        env_pass = os.environ.get("MEDIAFORGE_WEB_ADMIN_PASS", "").strip()
        if env_user and env_pass:
            create_user(env_user, env_pass, role="admin")
            logger.info("Auto-created admin user '%s' from environment", env_user)


# Once an admin exists there is no way back to "no admin at all" (the last
# admin cannot be deleted or demoted), so the True answer is permanent and
# worth caching: _check_setup() calls this before EVERY request, including the
# 40 image-proxy hits a browse page makes.
_has_admin_cached = False


def has_any_admin():
    global _has_admin_cached
    if _has_admin_cached:
        return True
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'"
        ).fetchone()
        found = bool(row["cnt"] > 0)
        if found:
            _has_admin_cached = True
        return found
    finally:
        conn.close()


def create_user(username, password, role="user", language=None):
    # language=None means "use the instance default" (Settings -> Design).
    # Resolving it here rather than at each call site is deliberate: the
    # admin user endpoint and the env bootstrap both used to fall through to
    # a hardcoded "en", which silently ignored the instance default.
    if language is None:
        language = get_setting("default_ui_language", "en")
    if language not in ("en", "de"):
        language = "en"
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, language) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), role, language),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_user_language(user_id: int) -> str:
    """Return the UI language code for a user ('en' or 'de'). Defaults to 'en'."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT language FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row and row["language"] in ("en", "de"):
            return row["language"]
        return "en"
    finally:
        conn.close()


def set_user_language(user_id: int, language: str) -> None:
    """Persist the UI language preference for a user."""
    if language not in ("en", "de"):
        language = "en"
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET language = ? WHERE id = ?", (language, user_id)
        )
        conn.commit()
    finally:
        conn.close()


def set_user_password(user_id, new_password) -> "tuple[bool, str | None]":
    """Replace a user's own password. Returns (ok, error).

    Deliberately does NOT check the old password: that belongs to the route,
    which is the layer that knows whether this is a self-service change (old
    password required) or an admin reset (it is not). Keeping the check out of
    here means neither case can accidentally inherit the other's rule.
    """
    if user_id is None:
        return False, "No user"
    password = str(new_password or "")
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), user_id),
        )
        conn.commit()
        if not cur.rowcount:
            return False, "User not found"
        return True, None
    except sqlite3.Error as exc:
        return False, str(exc)
    finally:
        conn.close()


def get_user_by_id(user_id):
    """The account row for *user_id* without its password hash, or None."""
    if user_id is None:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, role, auth_method, language, created_at "
            "FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def verify_user(username, password):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, role, auth_method FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            return None, "Invalid username or password."
        if row["auth_method"] != "local":
            return None, "This account uses SSO. Please use the SSO login button."
        if check_password_hash(row["password_hash"], password):
            return {
                "id": row["id"],
                "username": row["username"],
                "role": row["role"],
            }, None
        return None, "Invalid username or password."
    finally:
        conn.close()


def find_or_create_sso_user(
    issuer, subject, username, admin_username=None, admin_subject=None
):
    def _should_be_admin():
        # Subject-based promotion takes full priority — it is tied to the IdP
        # identity and cannot be spoofed by changing a display name.
        if admin_subject:
            return subject == admin_subject
        # Fall back to username only when no subject is configured.
        # This is weaker: warn once so admins know to upgrade.
        if admin_username and username == admin_username:
            logger.warning(
                "OIDC admin promotion matched by username '%s'. "
                "Configure OIDC_ADMIN_SUBJECT for stronger identity binding.",
                username,
            )
            return True
        return False

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, role FROM users WHERE sso_issuer = ? AND sso_subject = ?",
            (issuer, subject),
        ).fetchone()

        if row:
            user = {"id": row["id"], "username": row["username"], "role": row["role"]}
            if _should_be_admin() and row["role"] != "admin":
                conn.execute(
                    "UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],)
                )
                conn.commit()
                user["role"] = "admin"
            return user

        # Check for username conflict with local users
        existing = conn.execute(
            "SELECT id, auth_method FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if existing:
            raise ValueError(
                f"Username '{username}' is already taken by a local account."
            )

        role = "admin" if _should_be_admin() else "user"
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, auth_method, sso_subject, sso_issuer) "
            "VALUES (?, ?, ?, 'oidc', ?, ?)",
            (username, "", role, subject, issuer),
        )
        conn.commit()
        return {"id": cur.lastrowid, "username": username, "role": role}
    finally:
        conn.close()


def list_users():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, username, role, auth_method, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_user(user_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return False, "User not found"
        if row["role"] == "admin":
            cnt = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'"
            ).fetchone()["cnt"]
            if cnt <= 1:
                return False, "Cannot delete the last admin"
        # Purge every per-user table explicitly. The ON DELETE CASCADE on
        # user_notification_prefs never fires because PRAGMA foreign_keys is
        # off on every connection (see _configure_connection for why it has to
        # stay off), and push_subscriptions never declared the FK at all -- so
        # both tables used to outlive the account. SQLite reuses user ids by
        # value, which meant the next account created with this id inherited
        # the deleted user's notification prefs and push endpoints. The deletes
        # run before the users row so they are covered by the same commit, and
        # they stay correct if the pragma is ever switched on.
        for stmt in (
            "DELETE FROM user_notification_prefs WHERE user_id = ?",
            "DELETE FROM push_subscriptions WHERE user_id = ?",
            # Per-user too, and likewise without an FK: the hidden-request list
            # would otherwise be inherited by the next account with this id.
            "DELETE FROM seerr_hidden WHERE user_id = ?",
            # Group memberships have no FK either (the no-auth pseudo-user id 0
            # rules it out, same as user_ui_prefs), so a recycled user id would
            # otherwise inherit the deleted account's permissions -- the worst
            # variant of this bug, since it silently grants access.
            "DELETE FROM user_group_members WHERE user_id = ?",
        ):
            try:
                conn.execute(stmt, (user_id,))
            except sqlite3.Error:
                # Table may not exist yet on a fresh/partially migrated DB.
                logger.debug("delete_user: purge failed for %r", stmt, exc_info=True)
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        # user_ui_prefs has no FK to users (see its CREATE TABLE for why), so
        # the appearance rows would otherwise outlive the account and be
        # inherited by whoever gets this id next.
        clear_user_ui_prefs(user_id)
        return True, None
    finally:
        conn.close()


def update_user_role(user_id, new_role):
    if new_role not in USER_ROLES:
        return False, "Invalid role"
    conn = get_db()
    try:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return False, "User not found"
        if row["role"] == "admin" and new_role != "admin":
            cnt = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'"
            ).fetchone()["cnt"]
            if cnt <= 1:
                return False, "Cannot demote the last admin"
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
        conn.commit()
        return True, None
    finally:
        conn.close()
