"""get_setting_int(): a bad value must not take a worker down.

An unparsable MEDIAFORGE_* value used to raise ValueError, which threw the
auto-sync worker into its error branch on every cycle - sleep 30s, retry, never
sync - and made the settings page answer 500.
"""


def test_reads_the_db_value(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("sync_error_retries", "3")
        assert db.get_setting_int("sync_error_retries", 0, "MEDIAFORGE_SYNC_ERROR_RETRIES") == 3


def test_falls_back_to_the_environment(app, monkeypatch):
    from mediaforge.web import db
    monkeypatch.setenv("MEDIAFORGE_TEST_INT", "7")
    with app.app_context():
        assert db.get_setting_int("does_not_exist", 0, "MEDIAFORGE_TEST_INT") == 7


def test_bad_environment_value_falls_back_to_the_default(app, monkeypatch):
    from mediaforge.web import db
    monkeypatch.setenv("MEDIAFORGE_TEST_INT", "drei")
    with app.app_context():
        assert db.get_setting_int("does_not_exist", 5, "MEDIAFORGE_TEST_INT") == 5


def test_bad_db_value_falls_back_to_the_default(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("broken_number", "abc")
        assert db.get_setting_int("broken_number", 42) == 42


def test_empty_value_falls_back_to_the_default(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("empty_number", "")
        assert db.get_setting_int("empty_number", 9) == 9
