"""Relative imports must not point outside the package.

A relative import inside a FUNCTION is not checked until that line runs, so a
wrong level survives every import-time check and every test that stubs the
function out. web/catalogue_worker.py shipped with the depths a module in
web/routes/ needs -- `..db` meant `mediaforge.db`, `...providers` reached past
the top-level package -- and the bulk-download feature therefore died on its
first line for as long as it existed, in a background thread, where the
traceback only ever reached the log.

This walks the AST instead, so a mistake is caught whether or not the line is
ever executed.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = SRC / "mediaforge"

# Pre-existing and deliberately not fixed here: `mediaforge.aniskip` does not
# exist anywhere -- not in the package, not as a dependency in pyproject.toml.
# All four call sites sit behind MEDIAFORGE_ANISKIP=1, which is off by
# default, so the feature fails with ModuleNotFoundError only for whoever
# turns it on. Recorded rather than silently repaired because the right target
# is not knowable from here (the module looks to have been lost somewhere
# around the rename), and recorded rather than ignored so it cannot quietly
# grow a fifth call site.
KNOWN_MISSING = {
    ("mediaforge/models/aniworld_to/episode.py", "mediaforge.aniskip"),
    ("mediaforge/models/aniworld_to/series.py", "mediaforge.aniskip"),
    ("mediaforge/models/common/common.py", "mediaforge.aniskip"),
    # Same class again, found by this test: vendor/crypto.py is not in the
    # tree. Only reached when Crunchyroll sealed credentials are configured.
    ("mediaforge/vendor/crunchyroll_api.py", "mediaforge.vendor.crypto"),
}


def _modules():
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_relative_import_escapes_the_package():
    problems = []
    for path in _modules():
        # mediaforge/web/foo.py -> ["mediaforge", "web", "foo"]
        parts = path.relative_to(SRC).with_suffix("").parts
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        # A `from .` inside mediaforge/web/foo.py resolves against
        # mediaforge.web -- one level up. Inside a package's __init__.py it
        # resolves against that package itself, so those get one more.
        max_level = len(parts) if is_package else len(parts) - 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:                      # pragma: no cover
            problems.append("%s: %s" % (path.relative_to(SRC), exc))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and node.level > max_level:
                names = ", ".join(a.name for a in node.names)
                problems.append(
                    "%s:%d: 'from %s%s import %s' goes %d level(s) above "
                    "mediaforge (max %d here)"
                    % (path.relative_to(SRC), node.lineno, "." * node.level,
                       node.module or "", names, node.level - max_level, max_level))
    assert not problems, "relative imports outside the package:\n  " + "\n  ".join(problems)


def test_every_relative_import_points_at_a_module_that_exists():
    """The other half of the class, and the half that actually bit us.

    `from ..db import ...` inside mediaforge/web/ stays comfortably inside the
    package -- it just means mediaforge.db, which does not exist. Resolved
    against the filesystem rather than by importing, so every module in the
    tree is covered without running any of them.
    """
    problems = []
    for path in _modules():
        parts = path.relative_to(SRC).with_suffix("").parts
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        base = list(parts) if is_package else list(parts[:-1])
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:                              # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            anchor = base[:len(base) - (node.level - 1)] if node.level > 1 else list(base)
            if len(base) - (node.level - 1) < 0:
                continue                                 # already reported above
            target = anchor + ((node.module or "").split(".") if node.module else [])
            if not target:
                continue
            as_pkg = SRC.joinpath(*target) / "__init__.py"
            as_mod = SRC.joinpath(*target).with_suffix(".py")
            if as_pkg.exists() or as_mod.exists():
                continue
            # `from . import name` -- the name itself may be the submodule.
            if not node.module and all(
                    (SRC.joinpath(*anchor, a.name).with_suffix(".py").exists() or
                     (SRC.joinpath(*anchor, a.name) / "__init__.py").exists())
                    for a in node.names):
                continue
            rel = path.relative_to(SRC).as_posix()
            if (rel, ".".join(target)) in KNOWN_MISSING:
                continue
            problems.append("%s:%d: 'from %s%s import ...' -> no module %s"
                            % (path.relative_to(SRC), node.lineno, "." * node.level,
                               node.module or "", ".".join(target)))
    assert not problems, "relative imports with no target:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("module", [
    "mediaforge.web.catalogue_worker",
    "mediaforge.web.catalogue_store",
    "mediaforge.web.catalogue_ids",
])
def test_deferred_imports_actually_resolve(module):
    """Import-time is not enough for these: the names they need live in
    function bodies. Resolve each one explicitly."""
    import ast
    import importlib

    mod = importlib.import_module(module)
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    package = mod.__name__.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        target = importlib.import_module(
            "." * node.level + (node.module or ""), package)
        for alias in node.names:
            if hasattr(target, alias.name):
                continue
            # `from . import catalogue_ids` pulls in a SUBMODULE, which is not
            # an attribute of the package until something imports it.
            importlib.import_module(target.__name__ + "." + alias.name)
