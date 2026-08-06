"""Contracts between the shared browser globals and their callers.

Written after a bad afternoon: ops.js called ``MFScrollLock.acquire()`` and
``.release()``, which do not exist — the object exposes ``lock()``/``unlock()``.
The result was not a subtle glitch. ``openModal()`` calls ``closeModal()``
first, ``closeModal()`` threw on the very first line that touched the lock, and
so **every** "New rule / New group / New window / New API key" button in the
settings page did nothing at all, with one line in a console nobody had open.

Nothing in Python could catch that, so these tests read the JavaScript. They
are crude on purpose: a real check needs a browser, and a crude check that runs
in CI beats a thorough one that does not.
"""

import pathlib
import re

import pytest

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
