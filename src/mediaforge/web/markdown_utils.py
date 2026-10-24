"""Markdown rendering for Dev Info post bodies.

The remote Dev Info feed server allows full Markdown in the ``body`` field
(headings, bold/italic, lists, fenced code blocks, tables, links, images).
This module turns that Markdown source into sanitized HTML that is safe to
inject into the page (both server-rendered templates and the client-side
JS refresh path in static/devinfos.js), since the remote server is a
separate, unauthenticated app and its content must be treated as untrusted.
"""

import functools

import bleach
import markdown

# Tags the rendered Dev Info Markdown is allowed to produce. Covers standard
# prose/formatting elements plus tables and images -- everything the
# "fenced_code", "tables", "nl2br" and "sane_lists" extensions can emit.
ALLOWED_TAGS = [
    "p", "br", "strong", "em", "b", "i", "u", "s", "del",
    "ul", "ol", "li", "blockquote", "code", "pre",
    "span", "div", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    "a", "img",
    "table", "thead", "tbody", "tr", "th", "td",
]

# Allowed attributes per tag. The "*" key applies to every tag (e.g. the
# "class" bleach uses for fenced-code-block language spans/pre elements).
ALLOWED_ATTRS = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title"],
    "*": ["class"],
}


def render_markdown(text: str) -> str:
    """Render Markdown to sanitized HTML for safe display of Dev Info post bodies."""
    return _render_cached(text or "")


# The output depends on nothing but the input text, and the same few posts
# are rendered over and over: on every home page view (Dev-Info banners), on
# every Dev Infos page refresh and on every badge poll. markdown + bleach cost
# 1-20 ms per post on a fast CPU, several times that on a NAS.
@functools.lru_cache(maxsize=256)
def _render_cached(text: str) -> str:
    html = markdown.markdown(
        text,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
    )
    return bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
