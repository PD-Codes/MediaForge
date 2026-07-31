"""Load-order contract between library_core.js and the page modules.

A page module (library_video/books/comics.js) is loaded BEFORE the core,
because the core runs its init the moment it is parsed and needs LIB_KIND and
libRender() to already exist. The consequence is easy to forget: nothing in a
page module may CALL into the core at parse time.

Getting this wrong once cost a working shelf and did not look like a load
order problem at all -- a ReferenceError in a page module aborts the rest of
that file, so every `var` below the offending line stays undefined while the
function declarations around them are still hoisted. The visible symptom was
"the library could not be loaded", three files away.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web" / "static"

PAGE_MODULES = ["library_video.js", "library_books.js", "library_comics.js"]

# Defined by library_core.js, i.e. not available while a page module parses.
CORE_NAMES = [
    "_libPref", "_libSavePref", "_libPrefPatch", "_libInitialPerPage",
    "_libInitialView", "libFmtSize", "libEsc", "libEscAttr", "_libFauxArt",
    "volTagHtml", "libRenderPagination", "libTotalPages", "libApiPost",
    "libRegMenuCtx", "libCopyToClipboard", "libRepaint",
]


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in source.split("\n"))


@pytest.mark.parametrize("module", PAGE_MODULES)
def test_page_module_does_not_call_core_at_parse_time(module):
    source = _strip_comments((STATIC / module).read_text(encoding="utf-8"))

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
    source = _strip_comments((STATIC / module).read_text(encoding="utf-8"))
    assert re.search(r'^var LIB_KIND\s*=\s*"', source, re.M), \
        f"{module} must declare LIB_KIND as a literal"
    assert re.search(r"^var LIB_SORT_KEYS\s*=\s*\[", source, re.M), \
        f"{module} must declare LIB_SORT_KEYS as a literal"


@pytest.mark.parametrize("module", PAGE_MODULES)
def test_page_module_defines_what_the_core_calls(module):
    source = _strip_comments((STATIC / module).read_text(encoding="utf-8"))
    for required in ("libRender", "libUpdateSummary"):
        assert re.search(rf"^function {required}\s*\(", source, re.M), \
            f"{module} must define {required}(), the core calls it"
