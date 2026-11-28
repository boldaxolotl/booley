"""CI's lint-job check that a pull request adds no function over 50 lines.

Coding principle 1 caps a function at :data:`LIMIT` lines. A changed
``src/booley/`` function fails when it is new and longer than the limit, or
when it was already longer at the base and grew. Docstring lines and blank
lines do not count. Functions are matched to the base by qualified name
within their file.

This module depends only on the standard library.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from booley.dev_support.changed_line_coverage import CoverageGateError, merge_base

LIMIT = 50
_SCOPE = "src/booley"
_GIT_TIMEOUT_S = 60
_EXIT_UNUSABLE = 2

_Function = ast.FunctionDef | ast.AsyncFunctionDef


class FunctionLengthError(RuntimeError):
    """The check cannot be evaluated: Git state or a changed file is unusable."""


def function_lengths(source: str) -> list[tuple[str, int]]:
    """Return ``(qualified name, counted lines)`` for every function, in source order.

    A nested function is reported on its own and its lines also count in its
    parent.
    """
    found: list[tuple[str, int]] = []
    # Python's physical line breaks, not str.splitlines(): that also splits at U+2028,
    # form feeds and the like, which shifts every later AST line number.
    lines = re.split(r"\r\n|\r|\n", source)

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _Function):
                name = prefix + child.name
                found.append((name, _counted_lines(child, lines)))
                visit(child, name + ".<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(ast.parse(source), "")
    return found


def _counted_lines(node: _Function, lines: list[str]) -> int:
    span = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    first = node.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        span -= set(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return sum(1 for number in span if lines[number - 1].strip())


def violations(path: str, head: str, base: str | None) -> list[str]:
    """Return one message per function in ``head`` that is new and too long or grew."""
    before: dict[str, list[int]] = defaultdict(list)
    for name, length in function_lengths(base) if base is not None else ():
        before[name].append(length)
    seen: dict[str, int] = defaultdict(int)
    messages: list[str] = []
    for name, length in function_lengths(head):
        occurrence = seen[name]
        seen[name] += 1
        if length <= LIMIT:
            continue
        partners = before.get(name, [])
        if occurrence >= len(partners):
            messages.append(f"{path}: {name} is {length} lines (new; base: none); limit {LIMIT}")
        elif length > partners[occurrence]:
            messages.append(
                f"{path}: {name} is {length} lines (base: {partners[occurrence]}); "
                f"limit {LIMIT}, and it may not grow"
            )
    return messages


def check(root: Path, base: str) -> int:
    """Check the working tree's changed package files against ``base``; return an exit code."""
    listing = _git(
        root, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=d", base, "--", _SCOPE
    )
    paths = [item.decode() for item in listing.split(b"\0") if item.endswith(b".py")]
    problems: list[str] = []
    for path in paths:
        head = (root / path).read_text(encoding="utf-8")
        shown = subprocess.run(
            ("git", "show", f"{base}:{path}"),
            cwd=root,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        before = shown.stdout.decode() if shown.returncode == 0 else None
        try:
            problems.extend(violations(path, head, before))
        except SyntaxError as error:
            raise FunctionLengthError(f"{path} does not parse: line {error.lineno}") from error
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"function length: {len(paths)} changed source files checked against {base[:12]}")
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Check the checkout containing the current directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="base revision; default: newest merge base with main")
    args = parser.parse_args(argv)
    try:
        root = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        return check(root, args.base or merge_base(root))
    except (FunctionLengthError, CoverageGateError) as error:
        print(f"function length: {error}", file=sys.stderr)
        return _EXIT_UNUSABLE


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ("git", *args),
        cwd=root,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise FunctionLengthError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout
