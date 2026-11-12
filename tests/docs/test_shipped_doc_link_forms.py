"""Every shipped doc source uses only link forms the packaged-copy renderer can classify (#3657).

The build-stamp step renders the packaged copies without markdown-it, so a link form
its scanner cannot see (reference-style, autolink, link in an indented code block,
HTML ``href``) would ship unrewritten. This cross-check stays in the documentation-only
CI selection, which runs everything under ``tests/docs/``.
"""

from __future__ import annotations

from pathlib import Path

from booley.dev_support import reference_docs

ROOT = Path(__file__).resolve().parents[2]


def test_shipped_doc_link_forms_are_classifiable() -> None:
    for source in reference_docs.SHIPPED_DOCS:
        reference_docs.check_link_forms(source, (ROOT / source).read_bytes().decode("utf-8"))
