"""The download modal's three control fields must sit on one baseline.

Language, Hoster and Zielordner are `.mf-fld` boxes in one flex row. Two of
them label their control with a bare `<label>`; the language field wraps its
label in a `.mf-fld-labelrow` div so the "Mehrere" toggle can sit beside it.

That difference is the trap. forms.css styles the *element*:

    label { ... margin-bottom: 6px; }

A `<div>` does not get that margin, so the language field's `<select>` rode
6px higher than the other two. Copying the 6px onto the row fixed it exactly
once: a theme pack is injected after every core stylesheet and may set its own
`label` margin, at which point the hardcoded copy is wrong again.

So neither box carries a margin now. Both are one fixed-height line and the
spacing below comes from `.mf-fld`'s own `gap`, which makes the control row
independent of any global element rule. These tests guard the three properties
that were each, at some point, the reason the row was crooked -- verified
against a real browser (including simulated theme-pack overrides), then pinned
here because CI has no browser to re-measure with.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "mediaforge" / "web" / "static"


def _rules(css_text):
    """{normalised selector list: body} for every top-level rule.

    Matching the whole selector list matters: a bare `.mf-fld-labelrow` search
    would otherwise also hit the shared `.mf-fld-label, .mf-fld-labelrow` rule
    and read the wrong block. Comments are stripped first so a `}` inside one
    cannot end a rule early. At-rules (media queries) are skipped -- their
    contents are not the base layout this checks.
    """
    stripped = re.sub(r"/\*.*?\*/", "", css_text, flags=re.DOTALL)
    out = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", stripped):
        selector = " ".join(selector.split())
        if selector.startswith("@") or not selector:
            continue
        out.setdefault(selector, body)
    return out


def _block(css_text, selector):
    """The body of the rule whose full selector list is `selector`."""
    rules = _rules(css_text)
    assert selector in rules, f"no `{selector}` rule found — was it renamed?"
    return rules[selector]


def _px(block, prop):
    match = re.search(re.escape(prop) + r"\s*:\s*(-?[\d.]+)px", block)
    assert match, f"`{prop}` not set in:\n{block}"
    return float(match.group(1))


@pytest.fixture(scope="module")
def forms_css():
    return (STATIC / "forms.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def modals_css():
    return (STATIC / "modals.css").read_text(encoding="utf-8")


def test_the_global_label_margin_still_exists_to_be_neutralised(forms_css):
    """If it ever goes away, the `margin: 0` below is dead weight, not a fix."""
    assert _px(_block(forms_css, "label"), "margin-bottom") > 0


def test_neither_label_box_carries_a_margin(modals_css):
    """The two boxes must not depend on a global element rule to match.

    A bare <label> picks up forms.css's `label { margin-bottom }`; the
    .mf-fld-labelrow <div> that replaces it in the language field does not.
    Zeroing both and spacing with .mf-fld's `gap` is what keeps them equal even
    when a theme pack redefines the label margin.
    """
    shared = _block(modals_css, ".mf-fld-label, .mf-fld-labelrow")
    assert re.search(r"margin\s*:\s*0", shared), (
        "the shared label-area rule must zero the margin, otherwise the bare "
        "labels and the toggle row are spaced differently"
    )

    inner = _block(modals_css, ".mf-fld-labelrow .mf-fld-label")
    assert re.search(r"margin\s*:\s*0", inner), (
        "the caption inside .mf-fld-labelrow must zero its own margin too, or "
        "its text sits off-centre against the toggle beside it"
    )


def test_the_label_line_centres_its_contents(modals_css):
    """Caption and toggle have to sit on one line -- the row is what does it."""
    shared = _block(modals_css, ".mf-fld-label, .mf-fld-labelrow")
    assert "display: flex" in shared
    assert "align-items: center" in shared


def test_label_areas_have_one_fixed_height(modals_css):
    """`min-height` is not enough: a taller toggle would grow the row again."""
    block = _block(modals_css, ".mf-fld-label, .mf-fld-labelrow")
    assert "min-height" not in block, (
        "the shared label-area rule uses min-height; a toggle taller than that "
        "value grows the row and pushes the field's control out of line"
    )
    assert _px(block, "height") > 0


def test_the_toggle_stays_inside_the_label_area(modals_css):
    """A pill taller than the row it sits in is what broke the alignment."""
    row_height = _px(_block(modals_css, ".mf-fld-label, .mf-fld-labelrow"), "height")
    toggle_height = _px(_block(modals_css, ".mf-fld-toggle"), "height")
    assert toggle_height <= row_height, (
        "the .mf-fld-toggle is %gpx tall inside a %gpx label row"
        % (toggle_height, row_height)
    )
