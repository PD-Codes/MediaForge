"""Shared per-request helpers extracted from the old create_app() closure.

These used to be nested functions defined inside create_app() that closed
over a local ``auth_enabled`` variable; they are now plain module functions
that read the shared flag from runtime_state instead.
"""
from . import runtime_state


def anonymise_foreign_rows(items, keep_fields, owner_key="username"):
    """Own rows in full, other accounts' reduced to `keep_fields` + `foreign`.

    Shared by the three queues in the queue hub (downloads, encoding,
    upscaling). They all show one instance-wide, serial pipeline, so a row
    belonging to somebody else still has to be VISIBLE -- a wait with nothing
    apparently ahead of you looks like a stuck queue, and "when am I up?"
    becomes unanswerable. What it must not carry is what identifies the other
    person's viewing: title, file path, source URL, poster, error text.

    Admins get the rows untouched, and so does an auth-disabled instance
    (get_current_user_info() reports admin there).

    This is about disclosure only. Whether an account may ACT on a row is a
    separate check on the endpoint itself -- hiding a button has never stopped
    anyone from calling the API.
    """
    username, is_admin = get_current_user_info()
    if is_admin:
        return items

    out = []
    for item in items:
        owner = item.get(owner_key)
        if owner and owner == username:
            out.append(item)
            continue
        reduced = {key: item.get(key) for key in keep_fields}
        reduced["foreign"] = True
        out.append(reduced)
    return out


def get_current_user_info():
    """Return (username, is_admin) for the current request.

    When auth is disabled the app treats every request as an admin request.

    Used by: routes/settings.py and routes/syncplay.py (and several other
    route modules) to decide what the current request is allowed to do.
    """
    if not runtime_state.AUTH_ENABLED:
        return None, True  # no auth → treat as admin
    from .auth import get_current_user
    user = get_current_user()
    if not user:
        return None, False
    username = (
        user.get("username")
        if isinstance(user, dict)
        else getattr(user, "username", None)
    )
    role = (
        user.get("role")
        if isinstance(user, dict)
        else getattr(user, "role", "user")
    )
    return username, role == "admin"
