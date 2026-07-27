"""Cheap repository checks for CI (see .github/workflows/ci.yaml).

Each check is a subcommand and costs a second or two, so they can all run in
the lint job that gates the expensive build matrix:

    assets   every static file referenced from a template exists
    catalog  every compiled .mo matches its .po
    eol      no file is stored with CRLF against .gitattributes

Run without arguments to execute all of them. Exit code 1 on any failure.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "src" / "mediaforge" / "web"

# url_for('static', filename='x.css') and bare "/static/x.css" hrefs alike.
_URL_FOR = re.compile(r"""url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"]([^'"]+)['"]""")
_BARE = re.compile(r"""['"]/static/([^'"?#]+)""")
# HTML and Jinja comments: usage examples in there reference files that were
# never meant to exist (see mf_detail_modal.html's "image: /static/poster.jpg").
_COMMENT = re.compile(r"<!--.*?-->|\{#.*?#\}", re.DOTALL)


def _strip_comments(text):
    """Blank out comments but keep the line count, so line numbers stay right."""
    return _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def check_assets():
    """Every static file a template points at has to exist on disk.

    A typo in a <script src> only shows up as a silently missing feature in the
    browser, which is exactly the kind of thing nobody notices for weeks.
    """
    static_dir = WEB / "static"
    missing = []
    for tpl in sorted((WEB / "templates").rglob("*.html")):
        text = _strip_comments(tpl.read_text(encoding="utf-8", errors="replace"))
        for match in list(_URL_FOR.finditer(text)) + list(_BARE.finditer(text)):
            name = match.group(1)
            if "{" in name or "}" in name:
                continue        # built at render time, cannot be resolved here
            if not (static_dir / name).exists():
                line = text.count("\n", 0, match.start()) + 1
                missing.append(f"{tpl.relative_to(ROOT)}:{line}: missing static/{name}")
    return missing


def check_catalog():
    """A compiled .mo has to carry the same messages as its .po.

    Comparing the message maps rather than the bytes on purpose: msgfmt and
    pybabel produce different files for identical input, so a byte comparison
    would fail depending on who compiled last.
    """
    try:
        from babel.messages.mofile import read_mo
        from babel.messages.pofile import read_po
    except ImportError:
        return ["babel is not installed, cannot compare the translation catalogs"]

    problems = []
    for po_path in sorted((WEB / "translations").rglob("messages.po")):
        mo_path = po_path.with_suffix(".mo")
        if not mo_path.exists():
            problems.append(f"{po_path.relative_to(ROOT)}: no compiled messages.mo next to it")
            continue
        with po_path.open("rb") as fh:
            po = {m.id: m.string for m in read_po(fh) if m.id}
        with mo_path.open("rb") as fh:
            mo = {m.id: m.string for m in read_mo(fh) if m.id}
        stale = [mid for mid, text in po.items() if text and mo.get(mid) != text]
        if stale:
            sample = ", ".join(repr(s)[:60] for s in stale[:3])
            problems.append(
                f"{mo_path.relative_to(ROOT)}: {len(stale)} message(s) out of date "
                f"(e.g. {sample}) -- run pybabel compile"
            )
    return problems


def check_eol():
    """Nothing may be stored with CRLF unless .gitattributes says so.

    Mixed line endings in the index make every diff unreadable once a second
    editor touches the file. .gitattributes declares LF for everything except
    the Windows scripts, so anything else recorded as CRLF is drift.
    """
    out = subprocess.run(
        ["git", "ls-files", "--eol"], cwd=ROOT,
        capture_output=True, text=True, check=False,
    ).stdout
    bad = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].startswith("i/crlf"):
            continue
        name = parts[-1].strip()
        if name.endswith((".bat", ".ps1", ".cmd")):
            continue            # declared crlf in .gitattributes
        bad.append(f"{name}: stored with CRLF, expected LF")
    return bad


CHECKS = {"assets": check_assets, "catalog": check_catalog, "eol": check_eol}


def main(argv):
    wanted = argv[1:] or list(CHECKS)
    failed = False
    for name in wanted:
        check = CHECKS.get(name)
        if check is None:
            print(f"unknown check: {name}", file=sys.stderr)
            return 2
        problems = check()
        if problems:
            failed = True
            print(f"::group::{name}: {len(problems)} problem(s)")
            for problem in problems:
                print(f"  {problem}")
            print("::endgroup::")
        else:
            print(f"{name}: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
