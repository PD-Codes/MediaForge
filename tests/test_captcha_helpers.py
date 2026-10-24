"""Pure-Python parts of the captcha solver that decide what it trusts.

The browser-driven parts need a real browser and a real captcha; these do not,
and both have already been wrong in ways that mattered.
"""

import re

import pytest

from mediaforge.playwright import captcha

# captcha.py is a separately licensed component that modified distributions
# ship without; the package then installs a stub (see playwright/__init__.py).
pytestmark = pytest.mark.skipif(
    not hasattr(captcha, "_HUMAN_CHECKBOX_PATTERN"),
    reason="captcha solver component not part of this distribution",
)


@pytest.mark.parametrize("url, known", [
    ("https://voe.sx/e/abc", True),
    ("https://www.voe.sx/e/abc", True),
    ("https://delivery.voe.sx/e/abc", True),
    # lstrip("www.") stripped any leading run of "w" and "." characters, so
    # this lookalike was trusted as voe.sx.
    ("https://wvoe.sx/e/abc", False),
    ("https://voe.sx.example.invalid/e/abc", False),
    ("https://example.invalid/e/abc", False),
])
def test_known_provider_needs_the_real_domain(url, known):
    assert captcha._is_known_provider_url(url) is known


@pytest.mark.parametrize("text, is_robot_box", [
    ("I'm not a robot", True),
    ("Ich bin kein Roboter", True),
    ("Bestätigen Sie, dass Sie ein Mensch sind", True),
    ("Verify you are human", True),
    ("Captcha", True),
    # Substring matching took these for captcha checkboxes ("Angebot" and
    # "verboten" contain "bot"); the form then waited for them forever.
    ("Angebot merken", False),
    ("Das ist verboten", False),
    ("Newsletter abonnieren", False),
])
def test_robot_checkbox_keywords_match_whole_words(text, is_robot_box):
    # Same pattern the page-side scan compiles with new RegExp(pattern, 'i').
    found = re.search(captcha._HUMAN_CHECKBOX_PATTERN, text, re.IGNORECASE)
    assert bool(found) is is_robot_box


def test_submit_lookup_prefers_the_form_that_holds_the_widgets():
    """The generic "any visible submit button" entries come last and are not
    used in sub-frames, so a search or login form is never what gets submitted
    while the captcha form has a button of its own."""
    assert all(sel.startswith("form:has(") for sel in captcha._FORM_SUBMIT_SELECTORS)
    assert any("altcha-widget" in sel for sel in captcha._FORM_SUBMIT_SELECTORS)
    assert any("cf-turnstile-response" in sel for sel in captcha._FORM_SUBMIT_SELECTORS)
    assert not any(sel.startswith("form:has(") for sel in captcha._PAGE_SUBMIT_SELECTORS)
