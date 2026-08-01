"""The users-table rebuild that widens role's CHECK constraint.

Written after the first version of it emptied a live instance's users table.
The failure mode is worth spelling out, because it is not the one you would
guess: Python's sqlite3 module only opens a transaction before DML, so the
DDL in a rebuild (ALTER/CREATE/DROP) is committed the instant it runs.
`conn.rollback()` therefore undoes nothing, and a rebuild that fails halfway
leaves whatever the DDL already did.

These tests use a plain sqlite3 database rather than the app, so they run the
migration against the exact shapes a real installation can have.
"""

import sqlite3

import pytest


OLD_TABLE = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user')),
    auth_method TEXT NOT NULL DEFAULT 'local',
    sso_subject TEXT,
    sso_issuer TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@pytest.fixture()
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "t.db")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def _seed(conn, table_sql=OLD_TABLE, rows=3):
    conn.executescript(table_sql)
    for i in range(rows):
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("u%d" % i, "hash%d" % i, "admin" if i == 0 else "user"))
    conn.commit()


def test_the_rebuild_keeps_every_account(conn):
    from mediaforge.web.db import _migrate_role_check

    _seed(conn)
    _migrate_role_check(conn)

    rows = conn.execute("SELECT username, role FROM users ORDER BY id").fetchall()
    assert [r["username"] for r in rows] == ["u0", "u1", "u2"]
    assert rows[0]["role"] == "admin"
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone()["sql"]
    assert "'kids'" in sql
    # And the scratch table is gone rather than left lying around.
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'users_new_kids'").fetchone() is None


def test_a_database_that_predates_a_column_still_migrates(conn):
    """The original break: the old table had `language`, the CREATE statement
    the new table was built from did not, and copying column-for-column threw.
    Now only the columns BOTH tables have are copied."""
    from mediaforge.web.db import _migrate_role_check

    _seed(conn, OLD_TABLE.replace(
        "    language TEXT NOT NULL DEFAULT 'en',\n", ""))
    _migrate_role_check(conn)

    assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 3


def test_it_does_nothing_when_the_constraint_is_already_wide(conn):
    from mediaforge.web.db import _migrate_role_check

    _seed(conn, OLD_TABLE.replace("'admin', 'user'", "'admin', 'user', 'kids'"))
    ids_before = [r["id"] for r in conn.execute("SELECT id FROM users").fetchall()]
    _migrate_role_check(conn)
    assert [r["id"] for r in conn.execute("SELECT id FROM users").fetchall()] == ids_before


def test_the_migration_leaves_the_original_alone_when_the_copy_fails(conn, monkeypatch):
    """A failed rebuild must not touch the real table at all. The copy is
    verified BEFORE anything is dropped, which is the whole point."""
    from mediaforge.web import db as dbmod

    _seed(conn)
    # A CREATE statement the copy cannot satisfy: NOT NULL on a column the old
    # table has no value for.
    monkeypatch.setattr(dbmod, "_CREATE_TABLE", OLD_TABLE.replace(
        "CREATE TABLE users", "CREATE TABLE IF NOT EXISTS users").replace(
        "sso_subject TEXT", "sso_subject TEXT NOT NULL"))
    dbmod._migrate_role_check(conn)

    # Untouched: same rows, still under the same name.
    assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 3
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'users_new_kids'").fetchone() is None


# ---------------------------------------------------------------------------
# Recovering the databases the broken version already produced
# ---------------------------------------------------------------------------

def test_an_interrupted_rebuild_is_recovered_on_the_next_start(conn):
    """Exactly the state a live instance was left in: an EMPTY `users` table
    and every real account sitting in `users_old`. The instance asked for
    first-run setup; nothing was actually lost."""
    from mediaforge.web.db import _recover_interrupted_user_rebuild

    _seed(conn)
    conn.execute("ALTER TABLE users RENAME TO users_old")
    conn.executescript(OLD_TABLE.replace("'admin', 'user'", "'admin', 'user', 'kids'"))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0

    _recover_interrupted_user_rebuild(conn)

    rows = conn.execute("SELECT username FROM users ORDER BY id").fetchall()
    assert [r["username"] for r in rows] == ["u0", "u1", "u2"]
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'users_old'").fetchone() is None


def test_recovery_refuses_to_delete_the_newer_table_if_it_has_more_rows(conn):
    """A guess is not good enough when the alternative is deleting accounts:
    if the state does not match the known failure, both tables stay."""
    from mediaforge.web.db import _recover_interrupted_user_rebuild

    _seed(conn, rows=1)
    conn.execute("ALTER TABLE users RENAME TO users_old")
    conn.executescript(OLD_TABLE)
    for i in range(5):
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("newer%d" % i, "h"))
    conn.commit()

    _recover_interrupted_user_rebuild(conn)

    assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 5
    assert conn.execute("SELECT COUNT(*) AS c FROM users_old").fetchone()["c"] == 1


def test_recovery_is_a_no_op_on_a_healthy_database(conn):
    from mediaforge.web.db import _recover_interrupted_user_rebuild

    _seed(conn)
    _recover_interrupted_user_rebuild(conn)
    assert conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 3
