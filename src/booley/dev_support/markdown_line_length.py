"""Fail a branch that adds or changes a Markdown prose line over 80 columns.

Only lines the branch adds or changes are checked (against the newest merge
base with ``main``, or ``--base``), so existing long lines never fail. A line
is not prose, and never fails, inside a fenced code block, as a table row, an
ATX heading, YAML front matter or a generated block; when it is only a link,
URL or link reference definition; or when the only text past column 80 is one
URL or code span. The rule and its exemptions are
``docs/internals/DOCS_PRINCIPLES.md`` principle 17.

The module depends only on the standard library and on
:mod:`booley.dev_support.changed_line_coverage` for the merge base.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from booley.dev_support.changed_line_coverage import CoverageGateError, merge_base

MAX_COLUMNS = 80
_GIT_TIMEOUT_S = 60
_EXIT_FAILED = 1
_EXIT_UNUSABLE = 2
# Written by a generator, so a human cannot wrap them.
_GENERATED_PATHS = frozenset({"CHANGELOG.md", "src/booley/data/refs/CHANGELOG.md"})
# Snapshots of shipped guidance whose hashes a test pins.
_TEST_OWNED_PREFIXES = ("tests/fixtures/ticket_guidance/",)
# Changelog fragments move into the generated CHANGELOG.md and sit outside the
# docs principles' scope; the directory's README stays checked.
_FRAGMENT_PREFIX = "changelog.d/"
_FRAGMENT_README = "changelog.d/README.md"

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_QUOTE = re.compile(r"^(?: {0,3}>)*\s*")
_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^#{1,6}(?: |$)")
_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_LINK_ONLY = re.compile(
    r"^(?:<https?://[^>\s]+>|https?://\S+|!?\[[^\]]*\]\([^)\s]+\)|\[[^\]]*\]\[[^\]]*\])[.,;:]?$"
)
_LINK_DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*\S")
_TOKEN = re.compile(r"(`+)(?:(?!\1).)+?\1|https?://\S+")
_TRAILING_PUNCTUATION = ")]>.,;:!?*_'\""
_GENERATED_BEGIN = re.compile(r"^<!-- BEGIN GENERATED: (\S+) -->$")
_GENERATED_END = re.compile(r"^<!-- END GENERATED: (\S+) -->$")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-c", "core.quotePath=false", "-c", "diff.interHunkContext=0", *args),
        cwd=root,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise CoverageGateError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.decode("utf-8", errors="replace")


def _skipped(path: str) -> bool:
    if path in _GENERATED_PATHS or path.startswith(_TEST_OWNED_PREFIXES):
        return True
    return path.startswith(_FRAGMENT_PREFIX) and path != _FRAGMENT_README


def changed_lines(root: Path, base: str) -> dict[str, set[int]]:
    """Return the new-side line numbers the working tree adds to each Markdown file.

    Body lines are walked rather than taking each hunk header's range, so a
    user's ``diff.interHunkContext`` cannot pull an unchanged line in.
    """
    diff = _git(
        root,
        "diff",
        "-U0",
        "-M",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--diff-filter=AMR",
        base,
        "--",
        "*.md",
    )
    changed: dict[str, set[int]] = {}
    path: str | None = None
    number = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            if line.startswith('+++ "'):
                raise CoverageGateError(f"unexpected quoted path in diff: {line}")
            path = line[len("+++ b/") :]
            changed.setdefault(path, set())
        elif (hunk := _HUNK.match(line)) is not None:
            number = int(hunk.group(1))
        elif path is not None and line.startswith("+"):
            changed[path].add(number)
            number += 1
        elif path is not None and line.startswith(" "):
            number += 1
    return {name: lines for name, lines in changed.items() if not _skipped(name)}


def _token_reaches_end(line: str) -> bool:
    """Return whether one URL or code span starts by column 81 and ends the line."""
    stripped = line.rstrip(_TRAILING_PUNCTUATION)
    return any(
        match.start() <= MAX_COLUMNS and match.end() >= len(stripped)
        for match in _TOKEN.finditer(line)
    )


def _link_only(text: str) -> bool:
    return _LINK_ONLY.match(_LIST_MARKER.sub("", text, count=1)) is not None


def _exempt_line(line: str, text: str) -> bool:
    """Return whether a line outside a fence or front matter is not prose."""
    return (
        text.startswith("|")
        or _HEADING.match(text) is not None
        or _link_only(text)
        or _LINK_DEFINITION.match(line) is not None
        or _token_reaches_end(line)
    )


class _Block:
    """Fence, front matter and generated-block state while scanning top to bottom."""

    def __init__(self) -> None:
        self.fence: str | None = None
        self.front_matter = False
        self.generated: str | None = None

    def in_block(self, number: int, line: str, text: str) -> bool:
        """Advance on ``line`` and return whether it lies inside a block (edges included)."""
        if number == 1 and line == "---":
            self.front_matter = True
            return True
        if self.front_matter:
            self.front_matter = line not in {"---", "..."}
            return True
        if self.generated is not None:
            end = _GENERATED_END.match(line)
            if end is not None and end.group(1) == self.generated:
                self.generated = None
            return True
        begin = _GENERATED_BEGIN.match(line)
        if begin is not None:
            self.generated = begin.group(1)
            return True
        return self._fence(text)

    def _fence(self, text: str) -> bool:
        match = _FENCE.match(text)
        if self.fence is not None:
            closes = (
                match is not None
                and match.group(1)[0] == self.fence[0]
                and len(match.group(1)) >= len(self.fence)
                and not match.group(2).strip()
            )
            if closes:
                self.fence = None
            return True
        if match is not None and not (match.group(1)[0] == "`" and "`" in match.group(2)):
            self.fence = match.group(1)
            return True
        return False


def long_lines(text: str, changed: set[int]) -> list[tuple[int, int]]:
    """Return ``(line number, length)`` of each changed prose line over the limit.

    The whole file is scanned because block state depends on earlier lines.
    """
    block = _Block()
    found: list[tuple[int, int]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = _QUOTE.sub("", line, count=1)
        if block.in_block(number, line, stripped):
            continue
        if number in changed and len(line) > MAX_COLUMNS and not _exempt_line(line, stripped):
            found.append((number, len(line)))
    return found


def check(root: Path, base: str) -> int:
    """Check the branch's changed Markdown lines; return a process exit code."""
    changed = changed_lines(root, base)
    failures: list[str] = []
    checked = 0
    for path, lines in sorted(changed.items()):
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        checked += len(lines)
        failures += [
            f"{path}:{number}: {length} columns (limit {MAX_COLUMNS})"
            for number, length in long_lines(text, lines)
        ]
    if failures:
        print("\n".join(failures))
        print(
            f"Markdown line length: {len(failures)} changed line(s) over {MAX_COLUMNS} columns; "
            "wrap the prose (exemptions: docs/internals/DOCS_PRINCIPLES.md principle 17)"
        )
        return _EXIT_FAILED
    print(
        f"Markdown line length: {checked} changed line(s) in {len(changed)} file(s) "
        f"checked against {base[:12]}; none over {MAX_COLUMNS} columns"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Check the checkout containing the current directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="revision to compare with (default: merge base with main)")
    args = parser.parse_args(argv)
    try:
        root = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        return check(root, args.base or merge_base(root))
    except CoverageGateError as error:
        print(f"markdown line length: {error}", file=sys.stderr)
        return _EXIT_UNUSABLE
