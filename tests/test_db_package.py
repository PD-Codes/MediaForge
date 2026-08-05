"""Guard rails for the ``mediaforge.web.db`` package split.

The split of the former 6939-line ``db.py`` was a pure move, and these tests
exist so it stays one. Two things can quietly undo it:

* a function moving between submodules, or a new one being added to a
  submodule but not re-exported, which breaks ``from ..db import x`` at one
  of ~40 call sites — a runtime ImportError nobody sees until that page is
  opened;
* an import cycle, which the current layout does not have and which would
  otherwise be "fixed" by scattering lazy imports through the package.
"""

import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web" / "db"

# Names the package header binds for its own use. They were never public API
# even when everything lived in one file, and re-exporting them would make
# ``from ..db import json`` a supported thing to write.
_HEADER_NAMES = {
    "os", "re", "json", "sqlite3", "logger", "get_logger", "_dt",
    "check_password_hash", "generate_password_hash", "MEDIAFORGE_CONFIG_DIR",
    "_DEFAULT_MEDIA_KINDS", "annotations",
}


def _submodules():
    return sorted(p for p in PACKAGE.glob("*.py") if p.name != "__init__.py")


def _top_level_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names - _HEADER_NAMES


def test_package_is_not_empty():
    assert len(_submodules()) >= 15, "the db package lost its submodules"


def test_every_definition_is_re_exported():
    """``from ..db import x`` must keep working for everything the package defines."""
    from mediaforge.web import db

    missing = []
    for path in _submodules():
        for name in _top_level_names(path):
            if not hasattr(db, name):
                missing.append("%s.%s" % (path.stem, name))
    assert not missing, (
        "defined in a db submodule but not re-exported from db/__init__.py:\n  "
        + "\n  ".join(sorted(missing))
    )


def test_no_import_cycles():
    """A cycle would force lazy imports and make the load order load-bearing."""
    graph = {}
    for path in _submodules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        deps = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                deps.add(node.module)
        graph[path.stem] = deps

    state = {}
    cycles = []

    def visit(node, stack):
        state[node] = 1
        for dep in graph.get(node, ()):
            if state.get(dep) == 1:
                cycles.append(" -> ".join(stack + [node, dep]))
            elif dep in graph and state.get(dep) is None:
                visit(dep, stack + [node])
        state[node] = 2

    for module in graph:
        if state.get(module) is None:
            visit(module, [])

    assert not cycles, "import cycle(s) in the db package:\n  " + "\n  ".join(cycles)


def test_submodules_stay_reasonably_sized():
    """The point of the split. One file creeping back past ~1200 lines means a
    domain has grown enough to deserve its own module."""
    oversized = {
        path.name: sum(1 for _ in path.open(encoding="utf-8"))
        for path in _submodules()
        if sum(1 for _ in path.open(encoding="utf-8")) > 1200
    }
    assert not oversized, "db submodules that should be split further: %s" % oversized


@pytest.mark.parametrize("name", [
    # A spot-check across the domains, so a wholesale re-export failure is
    # reported as "get_setting is gone" rather than as 400 unrelated errors.
    "get_db", "DB_PATH", "init_db", "create_user", "verify_user", "USER_ROLES",
    "init_queue_db", "add_to_queue", "claim_next_queued", "cancel_queue_item",
    "get_setting", "set_setting", "get_json_setting", "set_json_setting",
    "is_sensitive_key", "register_sensitive_keys", "SENSITIVE_KEYS",
    "init_upscale_queue_db", "init_encoding_queue_db", "init_calendar_db",
    "get_uptime_heartbeats_between", "clear_user_ui_prefs",
])
def test_key_api_names_are_importable(name):
    from mediaforge.web import db
    assert hasattr(db, name), name


def test_old_single_file_module_is_gone():
    """A leftover db.py would be shadowed by the package and silently rot."""
    assert not (PACKAGE.parent / "db.py").exists()
