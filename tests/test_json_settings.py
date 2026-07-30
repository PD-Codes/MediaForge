"""get/set_json_setting(): a corrupt value must read as "unset", not raise.

List- and dict-valued settings used to be hand-encoded at every call site
(json.dumps on save, json.loads on read, each with its own or no try/except),
so one half-written or hand-edited row took down whatever read it.
"""


def test_round_trips_a_list(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_json_setting("json_list", ["a", "b"])
        assert db.get_json_setting("json_list", []) == ["a", "b"]


def test_round_trips_a_dict(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_json_setting("json_map", {"tok": 1})
        assert db.get_json_setting("json_map", {}) == {"tok": 1}


def test_missing_key_returns_the_default(app):
    from mediaforge.web import db
    with app.app_context():
        assert db.get_json_setting("json_never_written", []) == []
        assert db.get_json_setting("json_never_written") is None


def test_invalid_json_falls_back_to_the_default(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("json_broken", "{not json")
        assert db.get_json_setting("json_broken", []) == []


def test_empty_value_falls_back_to_the_default(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_setting("json_empty", "")
        assert db.get_json_setting("json_empty", {}) == {}


def test_wrong_container_type_falls_back_to_the_default(app):
    """A caller asking for a list and getting a dict would break one line later."""
    from mediaforge.web import db
    with app.app_context():
        db.set_json_setting("json_shape", {"a": 1})
        assert db.get_json_setting("json_shape", []) == []


def test_default_is_copied_not_shared(app):
    """A caller mutating the result must not poison the next caller's default."""
    from mediaforge.web import db
    default = []
    with app.app_context():
        got = db.get_json_setting("json_never_written_2", default)
        got.append("x")
        assert db.get_json_setting("json_never_written_2", default) == []


def test_non_ascii_is_stored_readably(app):
    from mediaforge.web import db
    with app.app_context():
        db.set_json_setting("json_umlaut", ["Grüße"])
        assert "Grüße" in db.get_setting("json_umlaut")
        assert db.get_json_setting("json_umlaut", []) == ["Grüße"]


def test_unserialisable_value_raises_at_the_write(app):
    from mediaforge.web import db
    with app.app_context():
        try:
            db.set_json_setting("json_bad_write", {1, 2})
        except TypeError:
            pass
        else:
            raise AssertionError("set_json_setting accepted a non-JSON value")
        assert db.get_setting("json_bad_write") is None
