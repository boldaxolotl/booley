"""Rendered shipped docs for tests, so none reads the build output directory (#3658).

The packaged copies under ``src/booley/data/docs/`` are Git-ignored build output: a
checkout may hold them, stale or not, or lack them. A test that checks shipped-doc text
renders each ``SHIPPED_DOCS`` source through the production renderer instead.
"""

from __future__ import annotations

from pathlib import Path

from booley.runtime.packaged_docs import (
    SHIPPED_DOCS,
    read_shipped_source,
    render_packaged_doc,
)

_USER_DOCS_PREFIX = "docs/user/"


def rendered_shipped_docs(root: Path) -> dict[str, str]:
    """Return the rendered text of every shipped doc, keyed by its repo-relative source path."""
    return {
        source: render_packaged_doc(source, read_shipped_source(root, source))
        for source in SHIPPED_DOCS
    }


def rendered_docs_outside_user_docs(root: Path) -> dict[str, str]:
    """Return the rendered shipped docs whose source is not under ``docs/user/``.

    Scanners that already read ``docs/user/`` and count matches per path add only these,
    so one source is never counted twice.
    """
    return {
        source: text
        for source, text in rendered_shipped_docs(root).items()
        if not source.startswith(_USER_DOCS_PREFIX)
    }
