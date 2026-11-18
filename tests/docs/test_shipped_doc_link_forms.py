"""Every shipped doc source uses only link forms the packaged-copy renderer can classify (#3657).

The build-stamp step renders the packaged copies without markdown-it, so a link form
its scanner cannot see (reference-style, autolink, link in an indented code block,
HTML ``href``) would ship unrewritten. This cross-check, which needs the third-party
parser the build path avoids, stays in the documentation-only CI selection, which runs
everything under ``tests/docs/``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from booley.runtime.packaged_docs import (
    SHIPPED_DOCS,
    UnclassifiedLinkError,
    link_destinations,
    read_shipped_source,
)

ROOT = Path(__file__).resolve().parents[2]
_HTML_HREF = re.compile(r"\bhref\s*=", re.IGNORECASE)


def _parser_destinations(source: str, text: str) -> list[str]:
    """Return markdown-it's link and image destinations; reject HTML links."""
    destinations: list[str] = []
    for token in MarkdownIt().parse(text):
        if token.type == "html_block" and _HTML_HREF.search(token.content):
            raise UnclassifiedLinkError(
                f"{source}: an HTML link (href=...) is a link form the rewriter cannot "
                "classify; use an inline Markdown link"
            )
        for child in token.children or []:
            if child.type == "html_inline" and _HTML_HREF.search(child.content):
                raise UnclassifiedLinkError(
                    f"{source}: an HTML link (href=...) is a link form the rewriter cannot "
                    "classify; use an inline Markdown link"
                )
            if child.type == "link_open":
                destinations.append(child.attrGet("href") or "")
            elif child.type == "image":
                destinations.append(child.attrGet("src") or "")
    return destinations


def check_link_forms(source: str, text: str) -> None:
    """Raise :class:`UnclassifiedLinkError` when markdown-it sees a link the scanner misses.

    The scanner reads inline links only, so a reference-style link, an autolink,
    a link in an indented code block or an HTML ``href`` would ship unrewritten.
    """
    normalize = MarkdownIt().normalizeLink
    scanned = [normalize(dest) for dest in link_destinations(text)]
    if scanned != _parser_destinations(source, text):
        raise UnclassifiedLinkError(
            f"{source}: contains a link form the rewriter cannot classify "
            "(reference-style, autolink, or link in an indented code block); "
            "use an inline Markdown link"
        )


def test_shipped_doc_link_forms_are_classifiable() -> None:
    for source in SHIPPED_DOCS:
        check_link_forms(source, read_shipped_source(ROOT, source))


def test_link_form_the_scanner_misses_fails() -> None:
    forms = [
        "[x][ref]\n\n[ref]: SETUP.md\n",
        "<https://booley.dev/docs/setup/>\n",
        "text\n\n    [x](SETUP.md)\n",
        '<a href="SETUP.md">x</a>\n',
        'Inline <a href="SETUP.md">x</a> link.\n',
    ]
    for text in forms:
        with pytest.raises(UnclassifiedLinkError, match="cannot classify"):
            check_link_forms("docs/user/CONFIG.md", text)
