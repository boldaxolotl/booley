"""Synchronize allowlisted user docs and the CHANGELOG into installed package data.

The authored docs live in ``docs/user/`` (plus ``docs/internals/FLOW_REPORTS.md``).
Installed runtimes and the Sandbox cannot rely on a repository checkout, so the
docs listed in :data:`SHIPPED_DOCS` are copied, flat, to ``src/booley/data/docs/``
(``/booley-docs/`` inside the Sandbox image). Run

    python -m booley.dev_support.reference_docs          # regenerate the copies
    python -m booley.dev_support.reference_docs --check  # fail on stale copies
    python .github/scripts/sync_packaged_copies.py [--check]  # same, without installing Booley

after editing any allowlisted doc or ``CHANGELOG.md``. The copies are generated;
never edit them. ``CHANGELOG.md`` is mirrored byte for byte (no link rewriting)
to ``src/booley/data/refs/CHANGELOG.md``, and ``--check`` covers that mirror too.

Links are rewritten so each copy is self-consistent in the flat directory:

* a link to a shipped doc (relative path, docs-site URL or Booley GitHub blob
  URL) becomes a sibling link ``<FILE>.md[#anchor]``;
* a link to a doc in :data:`PLAIN_TEXT_TARGETS` becomes plain text followed by
  :data:`NOT_SHIPPED_MARKER`;
* anchors, external URLs and fenced code are unchanged.

A link whose target is in neither set fails the sync, so adding a link from a
shipped doc forces a decision about its target.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

from booley.runtime.docs_links import USER_DOCS

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PACKAGED_DOCS_RELATIVE_DIR = "src/booley/data/docs"
CHANGELOG_SOURCE = "CHANGELOG.md"
PACKAGED_CHANGELOG_RELATIVE_PATH = "src/booley/data/refs/CHANGELOG.md"

# The one allowlist: repo-relative sources copied into the flat packaged directory.
SHIPPED_DOCS: tuple[str, ...] = (
    "docs/user/CONFIG.md",
    "docs/user/TARGETS.md",
    "docs/user/TESTS.md",
    "docs/user/FLOW-SIM.md",
    "docs/user/FLOW-LINT.md",
    "docs/user/FLOW-SYNTH.md",
    "docs/user/FLOW-FPGA.md",
    "docs/user/COVERAGE.md",
    "docs/user/SUPPORTED-EDA-TOOLS.md",
    "docs/user/TROUBLESHOOTING.md",
    "docs/user/SANDBOX.md",
    "docs/user/FLOW_REFERENCE.md",
    "docs/internals/FLOW_REPORTS.md",
)

# Explicit non-shipped link targets; a link to one becomes marked plain text.
PLAIN_TEXT_TARGETS: frozenset[str] = frozenset(
    {
        "docs/user/SETUP.md",
        "docs/user/USAGE.md",
        "docs/user/DOCTOR.md",
        "docs/user/STEALTH.md",
        "docs/user/GOAL-MODE.md",
        "docs/user/SPECIALISTS.md",
        "docs/user/CLI-REFERENCE.md",
        "docs/user/INSTALL.md",
        "docs/user/WINDOWS.md",
        "docs/internals/MCP-TOOLS.md",
        "docs/internals/ROADMAP.md",
        "docs/internals/FLOW_IMPLEMENTATION.md",
    }
)

# Appended to the text of a link to an unshipped doc; the citation guard reads it too.
NOT_SHIPPED_MARKER = "(host doc, not shipped)"

_SHIPPED_NAMES = tuple(posixpath.basename(source) for source in SHIPPED_DOCS)
assert len(set(_SHIPPED_NAMES)) == len(_SHIPPED_NAMES), "shipped doc basenames must be unique"

_SITE_HOSTS = frozenset({"booley.dev", "www.booley.dev"})
_SITE_DOCS_PREFIX = "/docs/"
_SLUG_TO_FILE = {doc.slug: doc.file for doc in USER_DOCS}
_GITHUB_BLOB = re.compile(r"^/boldaxolotl/booley/blob/[^/]+/(?P<path>.+)$", re.IGNORECASE)
_FENCE = re.compile(r"^ *(```+|~~~+)")
_HTML_HREF = re.compile(r"\bhref\s*=", re.IGNORECASE)
_REMEDY = (
    "add the target to SHIPPED_DOCS or PLAIN_TEXT_TARGETS in booley/dev_support/reference_docs.py"
)
_SYNC_COMMAND = "python -m booley.dev_support.reference_docs"


class UnclassifiedLinkError(ValueError):
    """A shipped doc links to a target the rewriter has no rule for."""


class _Link(NamedTuple):
    """One inline link or image found in prose, as offsets into the region text."""

    is_image: bool
    start: int  # first character of the link (``[`` or ``!``)
    text_end: int  # offset just past the link text's closing ``]``
    dest_start: int
    dest_end: int
    end: int  # offset just past the closing ``)``

    def destination(self, region: str) -> str:
        """Return the destination exactly as written."""
        return region[self.dest_start : self.dest_end]


def _skip_code_span(text: str, index: int) -> int:
    """Return the offset after the code span opening at ``index``, or just past its backticks."""
    run_end = index
    while run_end < len(text) and text[run_end] == "`":
        run_end += 1
    closing = re.compile(rf"(?<!`){'`' * (run_end - index)}(?!`)").search(text, run_end)
    return closing.end() if closing else run_end


def _closing_bracket(text: str, open_index: int) -> int | None:
    """Return the offset of the ``]`` balancing the ``[`` at ``open_index``."""
    depth = 0
    index = open_index
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            index = _skip_code_span(text, index)
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _destination_span(text: str, open_paren: int) -> tuple[int, int, int] | None:
    """Parse ``(dest "title")`` at ``open_paren``; return dest start, dest end, end offset."""
    index = open_paren + 1
    while index < len(text) and text[index].isspace():
        index += 1
    dest_start = index
    if index < len(text) and text[index] == "<":
        close = text.find(">", index)
        if close == -1 or "\n" in text[index:close]:
            return None
        dest_start, dest_end, index = index + 1, close, close + 1
    else:
        depth = 0
        while index < len(text) and not text[index].isspace():
            if text[index] == "\\":
                index += 1
            elif text[index] == "(":
                depth += 1
            elif text[index] == ")":
                if depth == 0:
                    break
                depth -= 1
            index += 1
        dest_end = index
    close = _closing_paren(text, index)
    if close is None or dest_start == dest_end:
        return None
    return dest_start, dest_end, close + 1


def _closing_paren(text: str, index: int) -> int | None:
    """Return the ``)`` ending a link after its destination, allowing an optional title."""
    title = re.compile(r"\s*(?:\"[^\"]*\"|'[^']*'|\([^)]*\))?\s*\)")
    match = title.match(text, index)
    return match.end() - 1 if match else None


def _scan_links(text: str) -> Iterator[_Link]:
    """Yield inline links and images of one prose region in document order."""
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == "`":
            index = _skip_code_span(text, index)
        elif char == "[" or (char == "!" and text.startswith("![", index)):
            is_image = char == "!"
            open_bracket = index + 1 if is_image else index
            close = _closing_bracket(text, open_bracket)
            span = (
                _destination_span(text, close + 1)
                if close is not None and text.startswith("(", close + 1)
                else None
            )
            if close is not None and span is not None:
                yield _Link(is_image, index, close + 1, *span)
            index = open_bracket + 1  # rescan inside the text: images nest in links
        else:
            index += 1


def _split_regions(text: str) -> list[tuple[str, bool]]:
    """Split ``text`` into ``(region, is_fenced_code)`` pieces, fences included verbatim."""
    regions: list[tuple[str, bool]] = []
    pending: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line)
        if fence is None and match:
            if pending:
                regions.append(("".join(pending), False))
                pending = []
            fence = match.group(1)
            pending.append(line)
        elif fence is not None:
            pending.append(line)
            if match and match.group(1).startswith(fence):
                regions.append(("".join(pending), True))
                pending, fence = [], None
        else:
            pending.append(line)
    if pending:
        regions.append(("".join(pending), fence is not None))
    return regions


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


def _repo_target(source: str, destination: str) -> tuple[str | None, str]:
    """Return ``(repo-relative target or None if external/anchor, raw fragment)``."""
    parts = urlsplit(destination)
    fragment = parts.fragment
    if not parts.scheme and not parts.netloc and not parts.path:
        return None, fragment  # in-page anchor
    if parts.scheme in {"http", "https"}:
        host = (parts.netloc or "").lower()
        if host in _SITE_HOSTS:
            return _site_target(source, destination, parts.path), fragment
        if host == "github.com":
            blob = _GITHUB_BLOB.match(unquote(parts.path))
            return (blob.group("path") if blob else None), fragment
        return None, fragment
    if parts.scheme or parts.netloc:
        return None, fragment  # mailto:, ftp:, ...
    if parts.path.startswith("/") or parts.query:
        raise UnclassifiedLinkError(
            f"{source}: link '{destination}' is unclassified; use a relative path"
        )
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(parts.path)))
    if resolved == ".." or resolved.startswith("../"):
        raise UnclassifiedLinkError(
            f"{source}: link '{destination}' escapes the repository; link a file inside it"
        )
    return resolved, fragment


def _site_target(source: str, destination: str, path: str) -> str:
    """Map a ``booley.dev/docs/<slug>/`` URL to its ``docs/user`` source."""
    slug = (
        path.removeprefix(_SITE_DOCS_PREFIX).strip("/")
        if path.startswith(_SITE_DOCS_PREFIX)
        else ""
    )
    if slug not in _SLUG_TO_FILE:
        raise UnclassifiedLinkError(
            f"{source}: link '{destination}' is a site page that is not a user doc; {_REMEDY}"
        )
    return f"docs/user/{_SLUG_TO_FILE[slug]}"


def _rewrite_region(source: str, region: str) -> str:
    """Rewrite the links of one prose region."""
    pieces: list[str] = []
    cursor = 0
    enclosing_end = 0  # end of the last rewritten link; later links starting inside nest in it
    for link in _scan_links(region):
        destination = link.destination(region)
        target, fragment = _repo_target(source, destination)
        if target is None:
            continue
        if link.start < enclosing_end:
            raise UnclassifiedLinkError(
                f"{source}: link '{destination}' ({target}) is nested in another doc link; "
                "the rewriter cannot classify it; unnest the link"
            )
        shipped = target in SHIPPED_DOCS
        if link.is_image or not (shipped or target in PLAIN_TEXT_TARGETS):
            raise UnclassifiedLinkError(
                f"{source}: link '{destination}' ({target}) is unclassified; {_REMEDY}"
            )
        pieces.append(region[cursor : link.start if not shipped else link.dest_start])
        if shipped:
            sibling = posixpath.basename(target) + (f"#{fragment}" if fragment else "")
            pieces.append(sibling)
            cursor = link.dest_end
        else:
            pieces.append(region[link.start + 1 : link.text_end - 1] + f" {NOT_SHIPPED_MARKER}")
            cursor = link.end
        enclosing_end = link.end
    pieces.append(region[cursor:])
    return "".join(pieces)


def render_packaged_doc(source: str, text: str) -> str:
    """Return the packaged form of one doc.

    ``source`` is its repo-relative path (links resolve against its directory);
    raises :class:`UnclassifiedLinkError` for a link the rules cannot classify.
    """
    scanned: list[str] = []
    for region, fenced in _split_regions(text):
        if not fenced:
            scanned.extend(link.destination(region) for link in _scan_links(region))
    normalize = MarkdownIt().normalizeLink
    if [normalize(dest) for dest in scanned] != _parser_destinations(source, text):
        raise UnclassifiedLinkError(
            f"{source}: contains a link form the rewriter cannot classify "
            "(reference-style, autolink, or link in an indented code block); "
            "use an inline Markdown link"
        )
    return "".join(
        region if fenced else _rewrite_region(source, region)
        for region, fenced in _split_regions(text)
    )


def _expected_copies(root: Path) -> dict[str, bytes]:
    """Return the packaged file name and bytes expected for every shipped doc."""
    return {
        posixpath.basename(source): render_packaged_doc(
            source, (root / source).read_bytes().decode("utf-8")
        ).encode("utf-8")
        for source in SHIPPED_DOCS
    }


def packaged_doc_problems(
    root: Path = _REPOSITORY_ROOT, packaged_dir: Path | None = None
) -> list[str]:
    """Return one line per missing, stale or orphaned packaged file."""
    directory = packaged_dir or root / PACKAGED_DOCS_RELATIVE_DIR
    expected = _expected_copies(root)
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


def sync_packaged_docs(
    root: Path = _REPOSITORY_ROOT, packaged_dir: Path | None = None
) -> list[str]:
    """Regenerate the packaged copies and delete orphans; return the changed names."""
    directory = packaged_dir or root / PACKAGED_DOCS_RELATIVE_DIR
    expected = _expected_copies(root)
    directory.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for name, content in expected.items():
        path = directory / name
        if not path.is_file() or path.read_bytes() != content:
            _atomic_write(path, content)
            changed.append(name)
    for path in sorted(directory.iterdir()):
        if path.name not in expected:
            path.unlink()
            changed.append(path.name)
    return changed


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
    _atomic_write(mirror, (root / CHANGELOG_SOURCE).read_bytes())
    return [PACKAGED_CHANGELOG_RELATIVE_PATH]


def _atomic_write(path: Path, content: bytes) -> None:
    """Publish one generated document without exposing a partial file."""
    temporary: Path | None = None
    try:
        mode = path.stat().st_mode if path.exists() else 0o644
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


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
    except UnclassifiedLinkError as error:
        print(error)
        return 1
    print(
        f"updated packaged docs: {', '.join(changed)}" if changed else "packaged docs are current"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
