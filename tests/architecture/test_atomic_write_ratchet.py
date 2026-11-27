"""Ratchet on staged-write-then-rename code outside ``booley.core.atomic_files``.

A site is a rename call in a scope (the module body, a class body, or one
function) that also writes a file. The count may only shrink.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

_SOURCE_ROOT = Path(__file__).parents[2] / "src" / "booley"
# The shared publisher and the retrying rename it is built on.
_OWNERS = frozenset({"core/atomic_files.py", "core/sharing_retry.py"})
_WRITES = frozenset(
    {"write_text", "write_bytes", "write", "mkstemp", "NamedTemporaryFile", "fdopen", "dump"}
)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

STAGED_WRITE_CEILING = 37


def _call_name(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Attribute):
        return function.attr
    return function.id if isinstance(function, ast.Name) else ""


def _is_rename(call: ast.Call) -> bool:
    name = _call_name(call)
    if name == "replace_file":
        return True
    function = call.func
    if name not in {"replace", "rename"} or not isinstance(function, ast.Attribute):
        return False
    if isinstance(function.value, ast.Name) and function.value.id == "os":
        return True
    # One argument is Path.replace/rename; str.replace and dataclasses.replace take more.
    keywords = [keyword.arg for keyword in call.keywords]
    return len(call.args) + len(keywords) == 1 and keywords in ([], ["target"])


def _scope_calls(scope: ast.AST) -> Iterator[ast.Call]:
    """Calls of one scope, without those of the functions and classes it nests."""
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, _SCOPES):
            continue
        if isinstance(node, ast.Call):
            yield node
        pending.extend(ast.iter_child_nodes(node))


def _staged_rename_lines(source: str) -> list[int]:
    tree = ast.parse(source)
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    scopes: list[ast.AST] = [tree, *(n for n in ast.walk(tree) if isinstance(n, nested))]
    lines: list[int] = []
    for scope in scopes:
        calls = list(_scope_calls(scope))
        if any(_call_name(call) in _WRITES for call in calls):
            lines.extend(call.lineno for call in calls if _is_rename(call))
    return sorted(lines)


def test_staged_write_then_rename_sites_stay_at_the_ceiling() -> None:
    sites: list[str] = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(_SOURCE_ROOT).as_posix()
        if relative not in _OWNERS:
            lines = _staged_rename_lines(path.read_text(encoding="utf-8"))
            sites.extend(f"{relative}:{line}" for line in lines)

    assert len(sites) <= STAGED_WRITE_CEILING, (
        f"{len(sites)} staged-write-then-rename sites, ceiling {STAGED_WRITE_CEILING}. "
        "Publish through booley.core.atomic_files instead:\n" + "\n".join(sites)
    )
    assert len(sites) == STAGED_WRITE_CEILING, f"lower STAGED_WRITE_CEILING to {len(sites)}"


@pytest.mark.parametrize(
    ("source", "count"),
    [
        ("t.write_text(x)\nreplace_file(t, p)\n", 1),
        ("fd, t = tempfile.mkstemp()\nos.replace(t, p)\n", 1),
        ("t.write_bytes(x)\nt.replace(target=p)\n", 1),
        (
            "def f():\n    t.write_text(x)\n    t.replace(p)\n    u.write_text(x)\n    u.rename(q)\n",
            2,
        ),
        ("class C:\n    t.write_text(x)\n    t.replace(p)\n", 1),
        ("path.rename(other)\n", 0),
        ("t.write_text(x)\ntext.replace('a', 'b')\n", 0),
    ],
    ids=[
        "replace_file",
        "mkstemp",
        "target-keyword",
        "two-in-one",
        "class-body",
        "lone-rename",
        "str-replace",
    ],
)
def test_detector_counts_staged_write_then_rename(source: str, count: int) -> None:
    assert len(_staged_rename_lines(source)) == count
