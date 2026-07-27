"""Shared pytest fixtures.

Everything here runs against a THROWAWAY config directory: MEDIAFORGE_CONFIG_DIR
is redirected to a temp dir before mediaforge is imported for the first time, so
a test run never touches the developer's real ~/.mediaforge (database, Flask
secret, image cache).
"""

import os
import sys
import tempfile
from pathlib import Path

# Must happen before the first mediaforge import: config.py reads the variable
# at module level.
_TMP_CONFIG_DIR = Path(tempfile.mkdtemp(prefix="mediaforge-tests-"))
os.environ["MEDIAFORGE_CONFIG_DIR"] = str(_TMP_CONFIG_DIR)
os.environ.setdefault("MEDIAFORGE_DOWNLOAD_PATH", str(_TMP_CONFIG_DIR / "downloads"))
# Keep the test run offline and quiet.
os.environ.setdefault("MEDIAFORGE_NO_UPDATE_CHECK", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def app():
    """The real Flask app, built exactly as production builds it."""
    from mediaforge.web.app import create_app

    application = create_app(auth_enabled=True)
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture(scope="session")
def users(app):
    """One admin and one regular account, created directly in the DB.

    Returns {"admin": id, "user": id}. Logging in through the form would drag
    in rate limiting and password hashing for no benefit -- the tests set the
    session directly (see as_user).
    """
    from mediaforge.web import db

    with app.app_context():
        admin_id = db.create_user("test-admin", "test-admin-pw-123", role="admin")
        user_id = db.create_user("test-user", "test-user-pw-123", role="user")
    return {"admin": admin_id, "user": user_id}


@pytest.fixture()
def as_user(client, users):
    """Log the test client in as "admin" or "user"."""

    def _login(role):
        with client.session_transaction() as sess:
            sess["user_id"] = users["admin" if role == "admin" else "user"]
            sess["user_name"] = "test-admin" if role == "admin" else "test-user"
            sess["user_role"] = "admin" if role == "admin" else "user"
        return client

    return _login
