#!/usr/bin/env python3
"""Fail a pull request whose test lines exceed twice its changed source lines.

Coding principle 18: more than two test lines per changed source line means
duplicated cases. The diff is taken against the merge base of ``--base`` and
``--head``. A pull request that removes more test lines than it adds passes, as
does one carrying the ``test-ratio-exempt`` label.
"""

from __future__ import annotations

import argparse
import bisect
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath

EXEMPT_LABEL = "test-ratio-exempt"
LINES_PER_SOURCE_LINE = 2
_DOC_ROOTS = frozenset({"docs", "changelog.d"})
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# One pass over Rust text: comments, strings and char literals are skipped so
# the braces and ``;`` that remain are code. A test item starts at an
# attribute that opens its line. Block comments nest, so ``_rust_comment_end``
# walks them.
_RUST_TOKEN = re.compile(
    r"""
    (?P<skip>
        //[^\n]*
      | (?<![A-Za-z0-9_])b?r(?P<hashes>\#*)".*?"(?P=hashes)
      | (?<![A-Za-z0-9_])b?"(?:\\.|[^"\\])*"
      | '(?:\\[^'\n]*|[^'\\\n])'
    )
  | (?P<comment>/\*)
  | (?P<attr>^[ \t]*\#\[cfg\(test\)\])
  | (?P<open>[{(\[])
  | (?P<close>[})\]])
  | (?P<semi>;)
    """,
    re.VERBOSE | re.DOTALL | re.MULTILINE,
)
_RUST_COMMENT_EDGE = re.compile(r"/\*|\*/")


@dataclass
class FileDiff:
    """Changed line numbers of one file: old side removed, new side added."""

    old_path: str
    new_path: str
    removed: list[int] = field(default_factory=list)
    added: list[int] = field(default_factory=list)


@dataclass
class Counts:
    """Line counts the verdict is drawn from."""

    test_added: int = 0
    test_deleted: int = 0
    source_changed: int = 0
    by_file: Counter[str] = field(default_factory=Counter)


def _header_path(line: str, prefix: str) -> str:
    path = line[4:].split("\t", maxsplit=1)[0]
    if path == "/dev/null":
        return path
    return path.removeprefix(prefix)


def _read_hunk(lines: list[str], index: int, diff: FileDiff) -> int:
    """Consume the hunk whose header is ``lines[index]``; return the next index."""
    match = _HUNK.match(lines[index])
    if match is None:
        raise ValueError(f"malformed hunk header: {lines[index]!r}")
    old, old_len, new, new_len = match.groups()
    old_line, new_line = int(old), int(new)
    remaining = int(old_len or 1) + int(new_len or 1)
    index += 1
    while remaining > 0 and index < len(lines):
        marker = lines[index][:1]
        if marker == "-":
            diff.removed.append(old_line)
            old_line += 1
        elif marker == "+":
            diff.added.append(new_line)
            new_line += 1
        elif marker != "\\":
            raise ValueError(f"hunk ends early at {lines[index]!r}")
        if marker != "\\":
            remaining -= 1
        index += 1
    return index


def parse_diff(text: str) -> list[FileDiff]:
    """Parse ``git diff -U0`` output into per-file changed line numbers."""
    lines = text.split("\n")
    diffs: list[FileDiff] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            diffs.append(FileDiff(_header_path(line, "a/"), _header_path(lines[index + 1], "b/")))
            index += 2
        elif line.startswith("@@") and diffs:
            index = _read_hunk(lines, index, diffs[-1])
        else:
            index += 1
    return diffs


def _rust_comment_end(text: str, position: int) -> int:
    """Return where the block comment opened just before *position* ends."""
    depth = 1
    while depth:
        edge = _RUST_COMMENT_EDGE.search(text, position)
        if edge is None:
            return len(text)
        depth += 1 if edge.group() == "/*" else -1
        position = edge.end()
    return position


def rust_test_lines(text: str) -> set[int]:
    """Return the 1-based lines inside ``#[cfg(test)]`` items of Rust *text*."""
    newlines = [match.start() for match in re.finditer("\n", text)]
    lines: set[int] = set()
    start = 0
    depth = 0
    braced = False
    active = False
    position = 0
    while token := _RUST_TOKEN.search(text, position):
        position = token.end()
        kind = token.lastgroup
        if kind == "comment":
            position = _rust_comment_end(text, position)
            continue
        line = bisect.bisect_left(newlines, token.start()) + 1
        if not active:
            if kind == "attr":
                active, start, depth, braced = True, line, 0, False
            continue
        depth += {"open": 1, "close": -1}.get(kind or "", 0)
        braced = braced or token.group() == "{"
        if (kind == "close" and braced and depth == 0) or (
            kind == "semi" and not braced and depth == 0
        ):
            lines.update(range(start, line + 1))
            active = False
    return lines


def _classify(path: str) -> str:
    """Return ``test``, ``rust``, ``source`` or ``ignored`` for a repository path."""
    pure = PurePosixPath(path)
    parents = pure.parts[:-1]
    if pure.suffix in {".md", ".lock"} or "vendor" in parents or pure.parts[0] in _DOC_ROOTS:
        return "ignored"
    if "tests" in parents:
        return "test" if pure.suffix in {".py", ".rs"} else "ignored"
    if pure.suffix == ".rs" and pure.parts[0] == "crates":
        return "rust"
    return "source"


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=repo,
        check=True,
        timeout=120,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def count_diff(repo: str, base: str, head: str) -> Counts:
    """Count test and source lines the diff from *base* to *head* changes."""
    diff = _git(
        repo, "diff", "-U0", "--find-renames", "--no-color", "--no-ext-diff", base, head, "--"
    )
    counts = Counts()
    for entry in parse_diff(diff):
        whole_deletion = entry.new_path == "/dev/null"
        for deleted, numbers, path, rev in (
            (True, entry.removed, entry.old_path, base),
            (False, entry.added, entry.new_path, head),
        ):
            kind = _classify(path)
            inside = (
                rust_test_lines(_git(repo, "show", f"{rev}:{path}"))
                if kind == "rust" and numbers
                else set()
            )
            for number in numbers:
                line_kind = ("test" if number in inside else "source") if kind == "rust" else kind
                if not (whole_deletion and line_kind == "source"):
                    _tally(counts, path, line_kind, deleted)
    return counts


def _tally(counts: Counts, path: str, kind: str, deleted: bool) -> None:
    if kind == "source":
        counts.source_changed += 1
    elif kind == "test" and deleted:
        counts.test_deleted += 1
    elif kind == "test":
        counts.test_added += 1
        counts.by_file[path] += 1


def verdict(counts: Counts, labels: set[str]) -> tuple[int, str]:
    """Return the exit code and the message for *counts*."""
    limit = LINES_PER_SOURCE_LINE * counts.source_changed
    if EXEMPT_LABEL in labels:
        return 0, f"exempt by the {EXEMPT_LABEL} label"
    if counts.test_deleted > counts.test_added or counts.test_added <= limit:
        return 0, ""
    top = "\n".join(f"  {path}: {n}" for path, n in counts.by_file.most_common(5))
    return 1, (
        f"{counts.test_added} test lines added for {counts.source_changed} changed source lines "
        f"exceeds the limit of {limit} (two per source line, coding principle 18).\n"
        f"Test lines added by file:\n{top}\n"
        f"Remove duplicated cases, or ask a maintainer to add the {EXEMPT_LABEL} label "
        "and re-run this job."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--labels", default="", help="newline-separated label names")
    args = parser.parse_args(argv)
    try:
        merge_base = _git(args.repo, "merge-base", args.base, args.head).strip()
        counts = count_diff(args.repo, merge_base, args.head)
    except (subprocess.CalledProcessError, ValueError) as error:
        print(f"error: {getattr(error, 'stderr', None) or error}", file=sys.stderr)
        return 2
    limit = LINES_PER_SOURCE_LINE * counts.source_changed
    print(
        f"test-ratio: {counts.test_added} test lines added, {counts.test_deleted} removed; "
        f"{counts.source_changed} source lines changed; limit {limit}."
    )
    code, message = verdict(counts, set(args.labels.splitlines()))
    if message:
        print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
