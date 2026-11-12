"""Synchronize the packaged user docs and the CHANGELOG mirror from the repository.

The allowlist, the link-rewriting rules and the renderer live in
``booley.runtime.packaged_docs``; every wheel and image builder renders the copies
through ``booley.runtime.build_stamp``. This module is the contributor-facing sync
CLI plus the markdown-it cross-check of the scanner, which needs a third-party
parser and so stays out of the build path. Run

    python -m booley.dev_support.reference_docs          # regenerate the copies
    python -m booley.dev_support.reference_docs --check  # fail on stale copies
    python3 .github/scripts/sync_packaged_copies.py [--check]  # same, without installing Booley

after editing any allowlisted doc. An ordinary change does not edit ``CHANGELOG.md``
or its mirror: it adds a fragment under ``changelog.d/`` that the release assembles
(``.github/scripts/release_changelog.py assemble``). The copies are generated;
never edit them. ``CHANGELOG.md`` is mirrored byte for byte (no link rewriting)
to ``src/booley/data/refs/CHANGELOG.md``, and ``--check`` covers that mirror too.

A shipped doc whose links the scanner and markdown-it read differently (reference
style, autolink, link in an indented code block, HTML ``href``) fails the sync.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from markdown_it import MarkdownIt

from booley.runtime.packaged_docs import (
    PACKAGED_DOCS_RELATIVE_DIR,
    SHIPPED_DOCS,
    MissingShippedDocError,
    UnclassifiedLinkError,
    atomic_write,
    expected_copies,
    link_destinations,
    read_shipped_source,
    write_packaged_docs,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CHANGELOG_SOURCE = "CHANGELOG.md"
PACKAGED_CHANGELOG_RELATIVE_PATH = "src/booley/data/refs/CHANGELOG.md"

_HTML_HREF = re.compile(r"\bhref\s*=", re.IGNORECASE)
_SYNC_COMMAND = "python -m booley.dev_support.reference_docs"


def _parser_destinations(source: str, text: str) -> list[str]:
    """Return markdown-it's link and image destinations; reject HTML links."""
    parser = MarkdownIt()
    destinations: list[str] = []
    for token in parser.parse(text):
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


def _check_shipped_link_forms(root: Path) -> None:
    """Run the cross-check on every shipped source."""
    for source in SHIPPED_DOCS:
        check_link_forms(source, read_shipped_source(root, source))


def packaged_doc_problems(root: Path = _REPOSITORY_ROOT) -> list[str]:
    """Return one line per missing, stale or orphaned packaged file."""
    _check_shipped_link_forms(root)
    directory = root / PACKAGED_DOCS_RELATIVE_DIR
    expected = expected_copies(root)
    problems: list[str] = []
    for name, content in expected.items():
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: packaged copy is missing")
        elif path.read_bytes() != content:
            problems.append(f"{name}: packaged copy is stale")
    if directory.is_dir():
        problems.extend(
            f"{path.name}: orphaned packaged file (no shipped source)"
            for path in sorted(directory.iterdir())
            if path.name not in expected
        )
    return problems


def sync_packaged_docs(root: Path = _REPOSITORY_ROOT) -> list[str]:
    """Regenerate the packaged copies and delete orphans; return the changed names."""
    _check_shipped_link_forms(root)
    return write_packaged_docs(root)


def packaged_changelog_problems(root: Path = _REPOSITORY_ROOT) -> list[str]:
    """Return one line when the packaged CHANGELOG mirror is missing or differs from the source."""
    mirror = root / PACKAGED_CHANGELOG_RELATIVE_PATH
    if not mirror.is_file():
        return [f"{PACKAGED_CHANGELOG_RELATIVE_PATH}: packaged copy is missing"]
    if mirror.read_bytes() != (root / CHANGELOG_SOURCE).read_bytes():
        return [f"{PACKAGED_CHANGELOG_RELATIVE_PATH}: packaged copy is stale"]
    return []


def sync_packaged_changelog(root: Path = _REPOSITORY_ROOT) -> list[str]:
    """Copy ``CHANGELOG.md`` byte for byte to its packaged mirror; return what changed."""
    if not packaged_changelog_problems(root):
        return []
    mirror = root / PACKAGED_CHANGELOG_RELATIVE_PATH
    mirror.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(mirror, (root / CHANGELOG_SOURCE).read_bytes())
    return [PACKAGED_CHANGELOG_RELATIVE_PATH]


def main(
    argv: list[str] | None = None,
    root: Path = _REPOSITORY_ROOT,
    sync_command: str = _SYNC_COMMAND,
) -> int:
    """Synchronize the packaged docs and CHANGELOG mirror, or check them without writing.

    ``sync_command`` is the fix command ``--check`` prints; each entry point names itself.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when a packaged doc copy or the CHANGELOG mirror is stale",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            problems = packaged_doc_problems(root) + packaged_changelog_problems(root)
            for problem in problems:
                print(problem)
            if problems:
                print(f"packaged docs are out of date; run {sync_command}")
            return 1 if problems else 0
        # Docs first: a bad link raises before anything is written.
        changed = sync_packaged_docs(root) + sync_packaged_changelog(root)
    except (UnclassifiedLinkError, MissingShippedDocError) as error:
        print(error)
        return 1
    print(
        f"updated packaged docs: {', '.join(changed)}" if changed else "packaged docs are current"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
