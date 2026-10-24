"""Web Push subscriptions.

Part of the ``mediaforge.web.db`` package -- see its ``__init__`` for why the
former single 6939-line ``db.py`` was split up and how the public API stayed
byte-for-byte identical.
"""

from ...logger import get_logger

from ._core import get_db

logger = get_logger(__name__)


def db_add_push_subscription(
    endpoint: str, auth: str, p256dh: str, user_id: "int | None" = None
) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, user_id, auth, p256dh) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(endpoint) DO UPDATE SET"
            "   user_id = excluded.user_id,"
            "   auth    = excluded.auth,"
            "   p256dh  = excluded.p256dh",
            (endpoint, user_id, auth, p256dh),
        )
        conn.commit()
    finally:
        conn.close()


def db_remove_push_subscription(endpoint, owner_id=None):
    """Delete the subscription for *endpoint*.

    With *owner_id* only a subscription of that account (or one stored
    without an owner) is deleted -- the unsubscribe route must not let one
    account remove another's. Without it (expired-endpoint cleanup) the row
    goes regardless of its owner.
    """
    conn = get_db()
    try:
        if owner_id is None:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        else:
            conn.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ?"
                " AND (user_id = ? OR user_id IS NULL)",
                (endpoint, owner_id),
            )
        conn.commit()
    finally:
        conn.close()


def db_get_push_subscriptions(user_id=None):
    conn = get_db()
    try:
        if user_id is not None:
            rows = conn.execute(
                "SELECT endpoint, user_id, auth, p256dh FROM push_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT endpoint, user_id, auth, p256dh FROM push_subscriptions"
            ).fetchall()
        return [
            {
                "endpoint": r["endpoint"],
                "user_id":  r["user_id"],
                "keys":     {"auth": r["auth"], "p256dh": r["p256dh"]},
            }
            for r in rows
        ]
    finally:
        conn.close()
