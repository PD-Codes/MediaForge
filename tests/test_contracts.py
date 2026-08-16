"""Repository-wide contracts that never start the app: file hygiene,
import direction, the browser globals' load order, the service worker,
and that every GET route answers.

Merged from: test_repo_hygiene.py, test_relative_imports.py, test_js_contracts.py, test_library_js_contract.py, test_pwa_offline.py, test_routes_smoke.py.
"""

import importlib.util
from pathlib import Path
import pytest
import ast
import pathlib
import re


# ==========================================================================
# test_repo_hygiene.py
#
# The repository checks CI runs, available locally through pytest as well.
# 
# .github/scripts/check_repo.py is the single implementation; this only wires it
# into the test suite so `pytest` catches the same things before a push does.
# The checks are pure file inspection -- no network, no app, milliseconds each.
# ==========================================================================
_SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "check_repo.py"


@pytest.fixture(scope="module")
def check_repo():
    if not _SCRIPT.is_file():
        pytest.skip("check_repo.py is missing (source checkout without .github/)")
    spec = importlib.util.spec_from_file_location("check_repo", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_templates_only_reference_existing_static_files(check_repo):
    problems = check_repo.check_assets()
    assert not problems, "\n  " + "\n  ".join(problems)


def test_translation_catalogs_are_compiled(check_repo):
    problems = check_repo.check_catalog()
    if problems and "babel is not installed" in problems[0]:
        pytest.skip(problems[0])
    assert not problems, "\n  " + "\n  ".join(problems)


def test_no_file_is_stored_with_crlf(check_repo):
    problems = check_repo.check_eol()
    assert not problems, "\n  " + "\n  ".join(problems)


# ==========================================================================
# test_relative_imports.py
#
# Relative imports must not point outside the package.
# 
# A relative import inside a FUNCTION is not checked until that line runs, so a
# wrong level survives every import-time check and every test that stubs the
# function out. web/catalogue_worker.py shipped with the depths a module in
# web/routes/ needs -- `..db` meant `mediaforge.db`, `...providers` reached past
# the top-level package -- and the bulk-download feature therefore died on its
# first line for as long as it existed, in a background thread, where the
# traceback only ever reached the log.
# 
# This walks the AST instead, so a mistake is caught whether or not the line is
# ever executed.
# ==========================================================================
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


# ==========================================================================
# test_js_contracts.py
#
# Contracts between the shared browser globals and their callers.
# 
# Written after a bad afternoon: ops.js called ``MFScrollLock.acquire()`` and
# ``.release()``, which do not exist — the object exposes ``lock()``/``unlock()``.
# The result was not a subtle glitch. ``openModal()`` calls ``closeModal()``
# first, ``closeModal()`` threw on the very first line that touched the lock, and
# so **every** "New rule / New group / New window / New API key" button in the
# settings page did nothing at all, with one line in a console nobody had open.
# 
# Nothing in Python could catch that, so these tests read the JavaScript. They
# are crude on purpose: a real check needs a browser, and a crude check that runs
# in CI beats a thorough one that does not.
# ==========================================================================
WEB = pathlib.Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web"
STATIC = WEB / "static"
BASE_HTML = WEB / "templates" / "base.html"


def _sources():
    """Every shipped script, minus the vendored ones we do not own."""
    vendored = ("hls.min.js", "pdf.min.js", "epub.min.js", "jszip.min.js",
                "qrcode.min.js")
    return [p for p in sorted(STATIC.glob("*.js")) if p.name not in vendored]


def _strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _members_of(global_name):
    """The keys the global's returned object literal exposes, from base.html."""
    html = BASE_HTML.read_text(encoding="utf-8")
    start = html.index("window.%s = (function" % global_name)
    body = html[start:start + 4000]
    ret = body.index("return {")
    literal = body[ret:body.index("})();", ret)]
    # `lock: lock,` / `unlock: function () {…}` / `get locked() {…}`
    return set(re.findall(r"(?:get\s+)?(\w+)\s*(?::|\()", literal)) - {"function"}


@pytest.mark.parametrize("global_name", ["MFScrollLock"])
def test_only_existing_members_of_shared_globals_are_called(global_name):
    exposed = _members_of(global_name)
    assert exposed, "could not read %s's public API out of base.html" % global_name

    bad = []
    for path in _sources():
        code = _strip_comments(path.read_text(encoding="utf-8"))
        for member in re.findall(r"%s\s*\.\s*(\w+)" % global_name, code):
            if member not in exposed:
                bad.append("%s: %s.%s()" % (path.name, global_name, member))

    assert not bad, (
        "calls into %s that do not exist (it exposes %s):\n  %s"
        % (global_name, ", ".join(sorted(exposed)), "\n  ".join(bad))
    )


def test_ops_js_uses_no_inline_onclick_with_interpolated_data():
    """Handlers are attached by delegation and read data-* attributes, so a
    value containing a quote cannot become code."""
    code = _strip_comments((STATIC / "ops.js").read_text(encoding="utf-8"))
    assert not re.search(r"onclick=\\?[\"'][^\"']*\"\s*\+", code), \
        "ops.js builds an inline onclick out of interpolated data"


def test_every_ops_entry_point_named_in_the_template_exists():
    """The settings page calls these by name from inline onclick attributes;
    a rename on either side is a button that silently does nothing."""
    template = (WEB / "templates" / "settings.html").read_text(encoding="utf-8")
    code = (STATIC / "ops.js").read_text(encoding="utf-8")

    called = set(re.findall(r'onclick="(ops[A-Za-z]+)\(', template))
    assert called, "no ops.js entry points are wired up in settings.html any more"

    missing = [name for name in sorted(called)
               if ("window.%s =" % name) not in code]
    assert not missing, "settings.html calls %s, ops.js does not define them" % missing


def test_dates_follow_the_app_language_not_the_browser():
    """`toLocaleString()` with no locale argument follows the BROWSER.

    That is a different setting from the app language and they disagree
    constantly -- a German UI on an en-US laptop was rendering MM/dd/yyyy on
    the Operations cards and dd.MM.yyyy in the download history, on the same
    machine, in the same session.
    """
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    for helper in ("window.mfLocale", "window.mfFormatDate",
                   "window.mfFormatTime", "window.mfFormatDateTime",
                   "window.mfFormatNumber"):
        assert helper + " = function" in base, "%s is gone from base.html" % helper

    # No date/number formatter may be left without a locale. pdf.min.js is a
    # vendored bundle and not ours to touch.
    bad = []
    for path in sorted(STATIC.glob("*.js")):
        if path.name == "pdf.min.js":
            continue
        code = _strip_comments(path.read_text(encoding="utf-8"))
        for call in re.findall(r"toLocale(?:Date|Time)?String\(\s*([^,)]*)", code):
            arg = call.strip()
            if arg == "" or arg == "undefined":
                bad.append("%s: toLocale…String(%s)" % (path.name, arg or "<empty>"))
    assert not bad, "formatters that follow the browser locale:\n  " + "\n  ".join(bad)


def test_only_the_module_manager_may_render_a_module_master_toggle():
    """A module's on/off switch belongs on exactly one page.

    Everywhere else it is a one-way door: switching a module off makes
    resolve_settings_cards() drop the card, so the control you just used
    vanishes and the way back is the module manager anyway.
    """
    macro = (WEB / "templates" / "_settings_card_macro.html").read_text(encoding="utf-8")
    assert "{% if show_master_toggle %}" in macro, \
        "the master toggle in _settings_card_macro.html is unconditional again"

    for name in ("integrations.html", "notifications.html", "monitoring.html",
                 "module_settings.html"):
        html = (WEB / "templates" / name).read_text(encoding="utf-8")
        assert "allow_module_toggle" not in html, \
            "%s opts back into rendering module master toggles" % name


def test_every_new_settings_tab_has_an_overview_card():
    """The overview grid is how most people reach a tab; a tab that is only in
    the side menu is one half the users never find."""
    menu = (WEB / "templates" / "_settings_menu.html").read_text(encoding="utf-8")
    settings = (WEB / "templates" / "settings.html").read_text(encoding="utf-8")

    tabs = set(re.findall(r"\(\'(\w+)\',\s*(?:_\(|\')", menu))
    tabs -= {"overview"}          # the overview does not link to itself
    tabs -= {"encoding"}          # its own /encoding page, linked separately

    carded = set(re.findall(r"switchTab\('(\w+)'\)", settings))
    missing = sorted(tabs - carded)
    assert not missing, "settings tabs with no overview card: %s" % missing


def test_syncplay_reaction_buttons_call_a_defined_function():
    """Same class of bug, other page: the reaction bar sat in the template
    calling a route that did not exist, and every tap was a silent 404."""
    template = (WEB / "templates" / "syncplay.html").read_text(encoding="utf-8")
    code = (STATIC / "syncplay_page.js").read_text(encoding="utf-8")
    if "SP.react(" in template:
        assert "S.react = function" in code, "SP.react is called but never defined"


def test_floating_reactions_have_a_positioned_container():
    """A position:absolute reaction inside a static parent resolves against the
    page and animates off-card -- which reads as "reactions do not work"."""
    css = (STATIC / "syncplay.css").read_text(encoding="utf-8")
    js = (STATIC / "syncplay_page.js").read_text(encoding="utf-8")

    hosts = re.findall(r"\$\('(sp\w+)'\)", js[js.index("function _floatReaction"):
                                              js.index("function _floatReaction") + 400])
    assert hosts, "could not tell where _floatReaction appends"

    for host in hosts:
        # spStage -> .sp-stage, spNow -> .sp-now
        selector = "." + re.sub(r"(?<!^)(?=[A-Z])", "-", host).lower()
        block = re.search(re.escape(selector) + r"\s*\{[^}]*\}", css)
        if block:
            assert "position: relative" in block.group(0), (
                "%s hosts floating reactions but is not positioned" % selector)


# ==========================================================================
# test_library_js_contract.py
#
# Load-order contract between library_core.js and the page modules.
# 
# A page module (library_video/books/comics.js) is loaded BEFORE the core,
# because the core runs its init the moment it is parsed and needs LIB_KIND and
# libRender() to already exist. The consequence is easy to forget: nothing in a
# page module may CALL into the core at parse time.
# 
# Getting this wrong once cost a working shelf and did not look like a load
# order problem at all -- a ReferenceError in a page module aborts the rest of
# that file, so every `var` below the offending line stays undefined while the
# function declarations around them are still hoisted. The visible symptom was
# "the library could not be loaded", three files away.
# ==========================================================================
LIB_STATIC = Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web" / "static"

PAGE_MODULES = ["library_video.js", "library_books.js", "library_comics.js"]

# Defined by library_core.js, i.e. not available while a page module parses.
CORE_NAMES = [
    "_libPref", "_libSavePref", "_libPrefPatch", "_libInitialPerPage",
    "_libInitialView", "libFmtSize", "libEsc", "libEscAttr", "_libFauxArt",
    "volTagHtml", "libRenderPagination", "libTotalPages", "libApiPost",
    "libRegMenuCtx", "libCopyToClipboard", "libRepaint",
]


def _lib_strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in source.split("\n"))


@pytest.mark.parametrize("module", PAGE_MODULES)
def test_page_module_does_not_call_core_at_parse_time(module):
    source = _lib_strip_comments((LIB_STATIC / module).read_text(encoding="utf-8"))

    # A top-level `var x = someCoreFn(...)` runs the moment the file is parsed.
    initialisers = re.findall(r"^(?:var|let|const)\s+\w+\s*=\s*([A-Za-z_$][\w$]*)\s*\(",
                              source, re.M)
    offenders = sorted({name for name in initialisers if name in CORE_NAMES})
    assert not offenders, (
        f"{module} calls {offenders} while it is being parsed, but "
        "library_core.js has not loaded yet"
    )


@pytest.mark.parametrize("module", PAGE_MODULES)
def test_page_module_declares_the_core_contract(module):
    """LIB_KIND and LIB_SORT_KEYS must be plain literals, for the same reason."""
    source = _lib_strip_comments((LIB_STATIC / module).read_text(encoding="utf-8"))
    assert re.search(r'^var LIB_KIND\s*=\s*"', source, re.M), \
        f"{module} must declare LIB_KIND as a literal"
    assert re.search(r"^var LIB_SORT_KEYS\s*=\s*\[", source, re.M), \
        f"{module} must declare LIB_SORT_KEYS as a literal"


@pytest.mark.parametrize("module", PAGE_MODULES)
def test_page_module_defines_what_the_core_calls(module):
    source = _lib_strip_comments((LIB_STATIC / module).read_text(encoding="utf-8"))
    for required in ("libRender", "libUpdateSummary"):
        assert re.search(rf"^function {required}\s*\(", source, re.M), \
            f"{module} must define {required}(), the core calls it"


# ==========================================================================
# test_pwa_offline.py
#
# The service worker's caching rules.
# 
# Parsed as text rather than executed: there is no service-worker runtime in
# pytest, and the properties worth guarding are all decisions visible in the
# source. The one that matters most is a *negative*: API responses must never be
# cached. A queue that claims three downloads are running while the server is
# unreachable, or a library listing full of files deleted this morning, is worse
# than an honest "you are offline" — stale operational data reads as truth.
# ==========================================================================
SW = (pathlib.Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web"
      / "static" / "sw.js")


@pytest.fixture(scope="module")
def source():
    return SW.read_text(encoding="utf-8")


def test_the_service_worker_actually_handles_fetches(source):
    """It used to cache two files and never serve them -- a PWA in name only,
    installable and completely blank the moment the network hiccupped."""
    assert 'addEventListener("fetch"' in source


def test_api_responses_are_never_cached(source):
    """The negative that matters. See the module docstring."""
    assert "isLiveData" in source
    body = source[source.index("function isLiveData"):]
    body = body[:body.index("}")]
    for path in ("/api/", "/healthz", "/readyz"):
        assert path in body, path
    # ...and the fetch handler has to bail out on them before any caching.
    handler = source[source.index('addEventListener("fetch"'):]
    assert re.search(r"if \(isLiveData\(url\)\) return;", handler)


def test_non_get_requests_are_left_alone(source):
    """A POST replayed from a cache would be a download queued twice."""
    handler = source[source.index('addEventListener("fetch"'):]
    assert re.search(r'request\.method !== "GET"\) return;', handler)


def test_range_requests_pass_through(source):
    """Video. The player relies on the server's own 206 handling, and a
    service worker "helping" here breaks seeking."""
    handler = source[source.index('addEventListener("fetch"'):]
    assert 'request.headers.has("range")' in handler


def test_only_complete_same_origin_responses_are_stored(source):
    """A 206 or an opaque cross-origin response cached here would later be
    served back as if it were the whole file."""
    checks = re.findall(r"response\.status === 200 && response\.type === \"basic\"", source)
    assert len(checks) >= 2, "both caching paths must apply the check"


def test_the_cache_is_versioned_and_old_ones_are_dropped(source):
    assert re.search(r'CACHE_VERSION = "[^"]+"', source)
    activate = source[source.index('addEventListener("activate"'):]
    assert "caches.delete" in activate


def test_shell_entries_are_added_individually(source):
    """addAll() is atomic: one renamed asset would reject the whole install,
    and a service worker that never installs never updates either."""
    install = source[source.index('addEventListener("install"'):
                     source.index('addEventListener("activate"')]
    # Comments stripped first -- the reason this is not addAll() is explained
    # in a comment that says "addAll", and a test that reads comments is a
    # test that fails on documentation.
    code = re.sub(r"//.*", "", install)
    assert "addAll" not in code
    assert "cache.add(url)" in code


def test_the_offline_page_is_part_of_the_shell(source):
    assert 'OFFLINE_URL = "/offline"' in source
    assert "OFFLINE_URL," in source          # listed in SHELL


def test_offline_page_answers_without_a_session(client, users):
    """Precached at install time, when the fetch carries no session worth
    relying on. A login redirect cached under this URL would be shown as
    "you are offline" forever.

    ``users`` is requested only so an admin exists: without one the app
    redirects everything to /setup, and this would fail for a reason that has
    nothing to do with the offline page.
    """
    resp = client.get("/offline")
    assert resp.status_code == 200
    assert b"MediaForge" in resp.data


def test_offline_page_carries_no_data(client, users):
    """Showing a cached queue here would be worse than showing nothing."""
    body = client.get("/offline").data.decode("utf-8", "replace").lower()
    # Words like "queue" and "library" legitimately appear in the sentence
    # explaining why nothing is shown, so the check is for the SHAPES data
    # would arrive in, not for the vocabulary.
    # "<li" without the closing bracket would match the <line> in the SVG.
    for marker in ("<table", "<ul", "<li>", "<li ", "browse-card", "queue-row"):
        assert marker not in body, marker
    assert "fetch(" not in body, "the offline page must not try to load data"
    assert "xmlhttprequest" not in body


# ==========================================================================
# test_routes_smoke.py
#
# Smoke test: every GET route answers without blowing up.
# 
# Cheap but effective on a codebase with 270+ routes and no tests: it catches
# template errors, missing imports inside view functions, typos in url_for() and
# anything else that only shows up when the route is actually called.
# 
# Nothing here asserts on content -- only that the server does not answer 500.
# ==========================================================================
def _plain_get_rules(app):
    """GET rules without URL parameters, minus the ones with side effects."""
    skip_endpoints = {
        "static",
        "service_worker",
        # Talks to the outside world or starts long-running work.
        "api_update_check",
        "api_store_catalog",
        "api_store_restart",
        "api_extensions_rescan",
        "api_mediascan_refresh",
        "api_library_refresh",
        "api_uptime_check_now",
        "api_image_proxy",
        "api_crunchyroll_availability",
        "api_fernsehserien_availability",
    }
    for rule in app.url_map.iter_rules():
        if rule.arguments or "GET" not in rule.methods:
            continue
        if rule.endpoint in skip_endpoints or rule.endpoint.startswith("auth."):
            continue
        yield rule


def test_there_are_routes(app):
    """Guards the test itself: an empty rule list would make everything pass."""
    assert len(list(_plain_get_rules(app))) > 50


# Statuses that mean "a third-party site let us down", not "this route is
# broken". The browse endpoints reach out to aniworld/megakino/filmpalast, so
# without them the suite would fail whenever a source site is unreachable --
# which, on CI, it regularly is. A crash still surfaces: an unhandled
# exception answers 500, and that is not in this set.
_UPSTREAM_STATUSES = {502, 503, 504}


def test_every_get_route_answers(app, as_user):
    """No route raises. 200/3xx/4xx are all fine -- 5xx is not."""
    client = as_user("admin")
    broken = []
    for rule in _plain_get_rules(app):
        try:
            resp = client.get(str(rule))
            if resp.status_code >= 500 and resp.status_code not in _UPSTREAM_STATUSES:
                broken.append(f"{rule.endpoint} ({rule}) -> {resp.status_code}")
        except Exception as exc:  # a raised exception is the same defect
            broken.append(f"{rule.endpoint} ({rule}) -> raised {type(exc).__name__}: {exc}")
    assert not broken, "routes failing with a server error:\n  " + "\n  ".join(broken)


def test_anonymous_requests_are_redirected_or_rejected(app, client):
    """Nothing but the documented exempt set answers without a session."""
    exempt_prefixes = ("/login", "/setup", "/auth/", "/static/", "/health", "/sw.js")
    leaked = []
    for rule in _plain_get_rules(app):
        path = str(rule)
        if path.startswith(exempt_prefixes):
            continue
        resp = client.get(path)
        if resp.status_code == 200:
            leaked.append(f"{rule.endpoint} ({path})")
    # /api/syncplay/rooms and the calendar feed are login-exempt by design;
    # they are listed in web/app.py's _exempt set with a reason.
    # Login-exempt by design; each is listed in web/app.py's _exempt set with a
    # reason (guests reach the SyncPlay lobby before they have an account).
    # /readyz joins api_health for the same reason: a container orchestrator,
    # a Docker HEALTHCHECK or an external uptime monitor has no session and
    # never will. It answers a single status string -- no version, no worker
    # names, no error text -- so an unauthenticated caller learns only whether
    # the process is up. (/healthz is already covered by the "/health" prefix
    # in exempt_prefixes above.)
    # api_v1_openapi is the machine-readable description of the external API.
    # A client cannot know which scopes to ask for until it can read the spec,
    # so requiring a key to fetch it is a chicken-and-egg problem. It describes
    # shapes -- endpoint names, parameter types, required scopes -- not data.
    # offline_page is precached by the service worker at install time, when
    # the fetch carries no session worth relying on. A login redirect cached
    # under that URL would then be shown as "you are offline" forever. The
    # page contains no data at all -- that is the whole point of it.
    known_exempt = {"api_syncplay_rooms", "api_syncplay_config", "api_health",
                    "api_calendar_ics", "syncplay_page", "readyz",
                    "api_v1_openapi", "offline_page"}
    unexpected = [x for x in leaked if x.split(" ")[0] not in known_exempt]
    assert not unexpected, "reachable without a login:\n  " + "\n  ".join(unexpected)
